# Eval Harness Report

## Mechanical Driver Scenarios (5 ground-truth fixtures)

```
============================= test session starts ==============================
collecting ... collected 18 items

tests/eval/test_eval_pipeline.py::test_volume_driven_driver_detected PASSED [  5%]
tests/eval/test_eval_pipeline.py::test_volume_driven_commentary_contains_driver PASSED [ 11%]
tests/eval/test_eval_pipeline.py::test_volume_driven_guard_passes PASSED [ 16%]
tests/eval/test_eval_pipeline.py::test_volume_driven_no_refusal PASSED   [ 22%]
tests/eval/test_eval_pipeline.py::test_margin_driven_driver_detected PASSED [ 27%]
tests/eval/test_eval_pipeline.py::test_margin_driven_commentary_contains_driver PASSED [ 33%]
tests/eval/test_eval_pipeline.py::test_margin_driven_guard_passes PASSED [ 38%]
tests/eval/test_eval_pipeline.py::test_margin_driven_no_refusal PASSED   [ 44%]
tests/eval/test_eval_pipeline.py::test_one_time_driver_detected PASSED   [ 50%]
tests/eval/test_eval_pipeline.py::test_one_time_commentary_contains_driver PASSED [ 55%]
tests/eval/test_eval_pipeline.py::test_one_time_guard_passes PASSED      [ 61%]
tests/eval/test_eval_pipeline.py::test_one_time_no_refusal PASSED        [ 66%]
tests/eval/test_eval_pipeline.py::test_mix_driver_detected_as_not_computable PASSED [ 72%]
tests/eval/test_eval_pipeline.py::test_mix_commentary_contains_hedge PASSED [ 77%]
tests/eval/test_eval_pipeline.py::test_mix_commentary_does_not_pick_definitive_driver PASSED [ 83%]
tests/eval/test_eval_pipeline.py::test_mix_no_refusal PASSED             [ 88%]
tests/eval/test_eval_pipeline.py::test_restatement_pipeline_refuses PASSED [ 94%]
tests/eval/test_eval_pipeline.py::test_restatement_never_calls_api PASSED [100%]

============================== 18 passed in 0.19s ==============================

```
