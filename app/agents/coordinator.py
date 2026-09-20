from __future__ import annotations

import uuid
from collections import defaultdict

from app.agents.autonomous import CoordinatorAgent
from app.agents.events import (
    AgentEvent,
    AgentEventType,
    AgentTask,
    CollaborationBlackboard,
    PRIORITY_ORDER,
    TaskPriority,
)
from app.agents.registry import AgentCapability, AgentRegistry
from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel


class EventDrivenCoordinator:
    """基于认领（claim）机制的协调器。

    本类只负责三件事：预算控制、任务派生、最终采纳策略。
    它不写死任何 Agent 执行顺序（没有固定 Agent 链），
    所有具体工作都由各 Agent 自主认领开放任务（OPEN task）后执行。
    """

    def __init__(self, registry: AgentRegistry, coordinator_agent: CoordinatorAgent, settings: Settings):
        self.registry = registry          # Agent 注册表：负责"任务能力匹配 + 候选决策"
        self.coordinator_agent = coordinator_agent  # 协调者 Agent：派生根任务、记住采纳结果
        self.settings = settings
        # —— 预算与采纳门槛（均可通过配置覆盖）——
        self.max_rounds = int(getattr(settings, "agent_max_rounds", 8))                    # 整个回合最多执行几轮
        self.max_claims_per_round = int(getattr(settings, "agent_max_claims_per_round", 4))  # 每轮最多认领几个任务
        self.max_claims_per_agent = int(getattr(settings, "agent_max_claims_per_agent", 3))   # 单个 Agent 整个回合最多认领几次
        self.final_min_confidence = float(getattr(settings, "agent_final_acceptance_min_confidence", 0.6))  # 最终采纳的最低置信度

    def run(self, board: CollaborationBlackboard) -> CollaborationBlackboard:
        """事件驱动多 Agent 协作主循环。

        每轮流程：
          1. 记录 ROUND_STARTED
          2. 根据黑板缺什么 artifact 派生任务（_derive_missing_work）
          3. 尝试最终采纳（_try_accept_final），成功则提前返回
          4. 让各 Agent 认领开放任务并执行（_claim_candidates + act）
          5. 执行结果写回黑板后，再派生新任务 / 再尝试采纳
        若轮次耗尽仍未采纳，追加 BUDGET_EXHAUSTED 事件后返回。
        """
        # 首次进入：黑板还没有任何任务，先由协调者创建根任务 task:root
        board = self._ensure_root_task(board)
        # 记录每个 Agent 本回合累计认领次数，用于限制单个 Agent 的调用预算
        claim_counts: dict[str, int] = defaultdict(int)
        for round_number in range(1, self.max_rounds + 1):
            # —— 1. 标记一轮开始 ——
            board = board.append_event(
                AgentEvent(
                    type=AgentEventType.ROUND_STARTED,
                    actor=self.coordinator_agent.name,
                    message=f"round={round_number}",
                    metadata={"round": round_number},
                )
            )
            # —— 2. 按需派生任务：黑板上缺 intent/risk/context/response 等就建对应任务 ——
            board = self._derive_missing_work(board)
            # —— 3. 前置采纳检查：若回复已通过 SafetyAgent 审查且置信度达标，直接采纳返回 ——
            board = self._try_accept_final(board)
            if board.final_artifact_id:
                return board
            # —— 4. 让 Agent 认领开放任务 ——
            candidates = self._claim_candidates(board, claim_counts)
            if not candidates:
                # 没有 Agent 认领（通常卡在"ResponseAgent 等 context"）：强制派生回复任务再试一次
                board = self._derive_missing_work(board, force_response=True)
                candidates = self._claim_candidates(board, claim_counts)
                if not candidates:
                    break  # 仍然无人认领，说明本轮无法推进，跳出循环走预算耗尽兜底
            # —— 5. 逐个执行被认领的任务 ——
            for task, candidate in candidates:
                # 任务可能在执行过程中被更新过，取黑板上的最新副本
                current_task = board.tasks.get(task.id, task)
                # 标记任务为 CLAIMED 并记录认领人，同时写入 TASK_CLAIMED 事件
                board = board.update_task(current_task.claim(candidate.agent.profile.name)).append_event(
                    AgentEvent(
                        type=AgentEventType.TASK_CLAIMED,
                        actor=candidate.agent.profile.name,
                        task_id=task.id,
                        message=candidate.decision.reason,
                        metadata={"confidence": candidate.decision.confidence},
                    )
                )
                # Agent 实际干活：返回 messages / artifacts / tasks / events
                result = candidate.agent.act(current_task, board)
                # 把 Agent 产出合并进黑板（发布 artifact、关闭/重开任务、追加事件等）
                board = board.apply_turn_result(current_task, candidate.agent.profile.name, result)
                claim_counts[candidate.agent.profile.name] += 1  # 累计该 Agent 认领次数
            # —— 6. 执行结果可能派生出新任务（如 SafetyAgent 打回重写），再次派生 + 采纳检查 ——
            board = self._derive_missing_work(board)
            board = self._try_accept_final(board)
            if board.final_artifact_id:
                return board
        # —— 预算耗尽兜底：追加事件后返回，由上层用 fallback 回复兜底，保证学生端不会拿不到回复 ——
        return board.append_event(
            AgentEvent(
                type=AgentEventType.BUDGET_EXHAUSTED,
                actor=self.coordinator_agent.name,
                message="event-driven agent budget exhausted before final acceptance",
            )
        )

    def _ensure_root_task(self, board: CollaborationBlackboard) -> CollaborationBlackboard:
        """确保黑板上有根任务；根任务是整个回合的占位入口，不直接被执行。"""
        if board.tasks:
            return board
        root = self.coordinator_agent.root_task(board)
        return board.add_task(root).append_event(
            AgentEvent(type=AgentEventType.TASK_CREATED, actor=self.coordinator_agent.name, task_id=root.id, message=root.title)
        )

    def _derive_missing_work(self, board: CollaborationBlackboard, force_response: bool = False) -> CollaborationBlackboard:
        """按需派生任务：黑板上缺哪种 artifact，就创建对应的任务。

        force_response=True 时，即使前置条件不满足也强制创建回复任务，
        用于"无 Agent 认领"时的兜底重试。
        """
        # 缺意图判定 -> 建 UnderstandingAgent 的任务（有用户输入才建）
        board = self._ensure_task_for_missing_artifact(
            board,
            artifact_kind="intent",
            task_id="task:understand",
            title="Understand user turn",
            capability=AgentCapability.UNDERSTANDING,
            priority=TaskPriority.HIGH,
            condition=board.user_input != "",
        )
        # 缺风险评估 -> 建 SafetyAgent 的任务；命中高风险硬词时升级为 CRITICAL
        board = self._ensure_task_for_missing_artifact(
            board,
            artifact_kind="risk",
            task_id="task:assess-safety",
            title="Assess safety risk",
            capability=AgentCapability.SAFETY,
            priority=TaskPriority.CRITICAL if _hard_high_risk(board.user_input) else TaskPriority.HIGH,
            condition=board.user_input != "",
        )
        # 动态路由：只有 咨询/风险 意图或 中/高 风险才需要检索上下文（记忆、RAG、skill）
        intent = _intent_value(board)
        risk = _risk_value(board)
        needs_context = intent in {IntentType.CONSULT, IntentType.RISK} or risk in {RiskLevel.MEDIUM, RiskLevel.HIGH}
        board = self._ensure_task_for_missing_artifact(
            board,
            artifact_kind="context",
            task_id="task:gather-context",
            title="Gather contextual evidence",
            capability=AgentCapability.CONTEXT,
            priority=TaskPriority.CRITICAL if risk == RiskLevel.HIGH else TaskPriority.NORMAL,
            condition=needs_context,
        )
        # 生成候选回复：需要先有 intent 和 risk；需要 context 的场景还必须先有 context
        has_response = board.latest_artifact("response_proposal") is not None
        can_request_response = force_response or (
            board.latest_artifact("intent") is not None
            and board.latest_artifact("risk") is not None
            and (not needs_context or board.latest_artifact("context") is not None or risk == RiskLevel.HIGH)
        )
        board = self._ensure_task_for_missing_artifact(
            board,
            artifact_kind="response_proposal",
            task_id="task:propose-response",
            title="Propose candidate response",
            capability=AgentCapability.RESPONSE,
            priority=TaskPriority.CRITICAL if risk == RiskLevel.HIGH else TaskPriority.HIGH,
            condition=can_request_response and not has_response,
        )
        # 安全审查：只要存在"尚未被审查"的回复，就建 SafetyAgent 的审查任务
        response = board.latest_artifact("response_proposal")
        review = board.latest_artifact("safety_review")
        critique = board.latest_artifact("critique")
        if response and (review is None or review.metadata.get("responseArtifactId") != response.id):
            board = self._ensure_task(
                board,
                AgentTask(
                    id=f"task:review-response:{response.id}",
                    title="Review candidate response safety",
                    description="Safety review is required before final acceptance.",
                    priority=TaskPriority.CRITICAL if risk == RiskLevel.HIGH else TaskPriority.HIGH,
                    required_capabilities=frozenset({AgentCapability.SAFETY.value}),
                    created_by=self.coordinator_agent.name,
                    metadata={"kind": "safety_review", "responseArtifactId": response.id},
                ),
            )
        # 打回重写：如果 SafetyAgent 给出的 critique 未被通过，建 CRITICAL 修订任务让 ResponseAgent 重写
        if critique and critique.payload.get("approved") is False:
            board = self._ensure_task(
                board,
                AgentTask(
                    id=f"task:revise-response:{critique.id}",
                    title="Revise response after critique",
                    description=str(critique.payload.get("reason", "Safety critique requested revision.")),
                    priority=TaskPriority.CRITICAL,
                    required_capabilities=frozenset({AgentCapability.RESPONSE.value}),
                    created_by=self.coordinator_agent.name,
                    metadata={"kind": "response", "revisionOf": critique.payload.get("responseArtifactId", "")},
                ),
            )
        return board

    def _ensure_task_for_missing_artifact(
        self,
        board: CollaborationBlackboard,
        artifact_kind: str,
        task_id: str,
        title: str,
        capability: AgentCapability,
        priority: TaskPriority,
        condition: bool,
    ) -> CollaborationBlackboard:
        """若满足 condition 且黑板上还没有该类型的 artifact，就创建对应任务。"""
        if not condition or board.latest_artifact(artifact_kind) is not None:
            return board
        return self._ensure_task(
            board,
            AgentTask(
                id=task_id,
                title=title,
                description=board.user_input,
                priority=priority,
                required_capabilities=frozenset({capability.value}),  # 只有具备该能力的 Agent 才能认领
                created_by=self.coordinator_agent.name,
                metadata={"kind": artifact_kind},
            ),
        )

    def _ensure_task(self, board: CollaborationBlackboard, task: AgentTask) -> CollaborationBlackboard:
        """幂等地把任务加入任务板（已有同名任务则跳过），并记录 TASK_CREATED 事件。"""
        if task.id in board.tasks:
            return board
        return board.add_task(task).append_event(
            AgentEvent(type=AgentEventType.TASK_CREATED, actor=self.coordinator_agent.name, task_id=task.id, message=task.title)
        )

    def _claim_candidates(self, board: CollaborationBlackboard, claim_counts: dict[str, int]):
        """让 Agent 认领开放任务，返回本轮要执行的 (任务, 认领者) 列表。

        约束：
        - 每个 Agent 整个回合认领次数不超过 max_claims_per_agent
        - 每轮认领的任务数不超过 max_claims_per_round
        - 同一 Agent 每轮最多认领一个任务；同一任务-Agent 组合只认领一次
        排序：优先级高的任务优先，其次认领置信度高者优先，再按 Agent 名稳定排序。
        """
        selected = []
        task_candidates = []
        # 收集所有开放任务的候选认领者（Registry 已做能力匹配 + decide() 过滤）
        for task in board.open_tasks():
            for candidate in self.registry.candidate_decisions_for(task, board):
                # 超出单 Agent 认领预算的直接跳过
                if claim_counts[candidate.agent.profile.name] >= self.max_claims_per_agent:
                    continue
                task_candidates.append((task, candidate))
        # 按 (优先级, 置信度, Agent名) 降序排序：CRITICAL 任务、高置信度 Agent 优先
        task_candidates.sort(
            key=lambda item: (
                PRIORITY_ORDER[item[0].priority],
                item[1].decision.confidence,
                item[1].agent.profile.name,
            ),
            reverse=True,
        )
        # 去重 + 限制每轮认领数量
        seen = set()
        selected_agents = set()
        for task, candidate in task_candidates:
            key = (task.id, candidate.agent.profile.name)
            # 同一任务-同一 Agent 只算一次；同一 Agent 本轮只认领一个任务
            if key in seen or candidate.agent.profile.name in selected_agents:
                continue
            selected.append((task, candidate))
            seen.add(key)
            selected_agents.add(candidate.agent.profile.name)
            if len(selected) >= self.max_claims_per_round:
                break
        return selected

    def _try_accept_final(self, board: CollaborationBlackboard) -> CollaborationBlackboard:
        """最终采纳门槛。必须同时满足：
        1. 已存在回复 proposal 与 safety_review
        2. safety_review 审查的正是这条回复（responseArtifactId 匹配）
        3. SafetyAgent 审查结论为 approved
        4. 回复置信度不低于 final_min_confidence
        通过后由 Coordinator 采纳并记录 FINAL_ACCEPTED 事件。
        """
        if board.final_artifact_id:
            return board
        response = board.latest_artifact("response_proposal")
        review = board.latest_artifact("safety_review")
        if response is None or review is None:
            return board
        if review.metadata.get("responseArtifactId") != response.id:
            return board
        if not review.payload.get("approved"):
            return board
        if response.confidence < self.final_min_confidence:
            return board
        reason = "accepted after autonomous response proposal and SafetyAgent approval"
        self.coordinator_agent.remember_acceptance(response.id, reason)
        return board.accept_final(response.id, self.coordinator_agent.name, reason)


def _intent_value(board: CollaborationBlackboard) -> IntentType:
    """读取黑板上的最新意图；无意图 artifact 时根据高风险硬词兜底为 RISK。"""
    artifact = board.latest_artifact("intent")
    if artifact:
        try:
            return IntentType(str(artifact.payload.get("intent", IntentType.CHAT.value)).upper())
        except ValueError:
            return IntentType.CHAT
    if _hard_high_risk(board.user_input):
        return IntentType.RISK
    return IntentType.CHAT


def _risk_value(board: CollaborationBlackboard) -> RiskLevel:
    """取所有 risk artifact 中的最高风险等级；出现 SAFETY_OVERRIDE 事件时强制为 HIGH。"""
    order = {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}
    highest = RiskLevel.LOW
    for artifact in board.artifacts_by_kind("risk"):
        try:
            risk = RiskLevel(str(artifact.payload.get("risk", RiskLevel.LOW.value)).upper())
        except ValueError:
            risk = RiskLevel.LOW
        if order[risk] > order[highest]:
            highest = risk
    if any(event.type == AgentEventType.SAFETY_OVERRIDE for event in board.events):
        return RiskLevel.HIGH
    return highest


def _hard_high_risk(text: str) -> bool:
    """高风险硬词匹配：命中即认为存在高危自伤信号（快速兜底，不等 LLM 评估）。"""
    lowered = (text or "").lower()
    return any(word in lowered for word in ["自杀", "自残", "不想活", "结束生命", "伤害自己", "轻生", "suicide", "kill myself", "self harm"])
