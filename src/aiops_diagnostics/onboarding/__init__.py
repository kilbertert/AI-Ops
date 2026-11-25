"""环境接入与候选发布编排包（ADR-0339）。

收敛原分散在 gateway/、dify/、publication/、consumer/ 的环境目录、草稿、计划、
任务状态机、SSH 远程执行器、候选验证试跑与正式发布模块，并保持全包严格无环分层：
registry/artifacts/drafts/plans/tasks/context -> preparation/ssh_runners/trial_runtime ->
installation/registration/validation/publication/orchestration/overview -> management/trial。
"""
