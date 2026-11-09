"""RSI owns multiple reference work clocks, not one baseline-sized trial clock."""


def install_rsi_budget_support():
    from .pier_budget import install_work_budget_support
    install_work_budget_support()
    from pier.trial.execution import TrialExecution
    original = TrialExecution.run_agent
    if getattr(original, "_rsi_phase_budgets", False):
        return

    async def run_agent(self, *, instruction, context):
        from .rsi import BitfunRSI
        if not isinstance(self.agent, BitfunRSI):
            return await original(self, instruction=instruction, context=context)
        # Full DRS has no project cap or global convergence timer. Individual
        # roles and target attempts retain their undisclosed working ceilings.
        await self.agent.run(instruction, self.environment, context)

    run_agent._rsi_phase_budgets = True
    # Retain the inner baseline adapter marker so later baseline construction
    # cannot repeatedly wrap this dispatcher.
    run_agent._baseline_work_budget = True
    TrialExecution.run_agent = run_agent
