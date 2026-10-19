"""行为回归评测。

- `evals/harness.py`：纯逻辑（用例加载、断言、聚合、基线对比），不依赖 app/网络；
- `evals/runner.py`：执行器，走生产事件链路把一条用例跑成可断言的结果；
- `evals/cases.yaml`：用例集；
- `scripts/run_eval.py`：CLI 入口。

用法见 README「评测」一节。运行产物落在 `evals/runs/`（已忽略，不进版本库）。
"""
