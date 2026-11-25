"""HTTP装配、作业调度、配置、存储和可观测性。

环境接入全流程（登记、草稿、计划、任务状态机、SSH 执行器 ``*_ssh_runner.py``、
候选试跑与正式发布）已垂直收敛至 ``aiops_diagnostics.onboarding``（ADR-0339）；
本包仅保留 Gateway HTTP 装配、路由、配置、持久化存储、异步调度与可观测性。
"""
