"""Narrow timeout adapter for the pinned Pier single-task execution path."""

from .work_budget import WorkBudget


def install_work_budget_support():
    from pier.trial.execution import AgentTimeoutError, TrialExecution

    original = TrialExecution.run_agent
    if getattr(original, "_baseline_work_budget", False):
        return

    async def run_agent(self, *, instruction, context):
        from .baseline import BitfunBaseline

        if not isinstance(self.agent, BitfunBaseline):
            return await original(self, instruction=instruction, context=context)
        budget = WorkBudget(self.agent.total_timeout_sec)
        self.agent._work_budget = budget
        outer = None
        try:
            if self.agent_timeout_sec is None:
                await self.agent.run(instruction, self.environment, context)
            else:
                async with budget.limit(self.agent_timeout_sec) as outer:
                    await self.agent.run(instruction, self.environment, context)
        except TimeoutError as error:
            if outer is None or not outer.expired():
                raise
            raise AgentTimeoutError(
                f"Agent execution exceeded {self.agent_timeout_sec} working seconds"
            ) from error

    run_agent._baseline_work_budget = True
    TrialExecution.run_agent = run_agent
