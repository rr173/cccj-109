"""pytest 封装：随机输入 vs 逐日枚举参考 + 不变量核对。"""

import pytest

from pipeline import checks
from pipeline.cohort import derive_cohort
from pipeline.estimate import estimate_groups
from pipeline.loaders import devices_from_rows, events_from_rows
from pipeline.reference import reference_estimate
from pipeline.synth import generate


@pytest.mark.parametrize("seed", range(1, 21))
def test_random_matches_daily_reference(seed):
    devices_rows, events_rows, obs_end, cutoff = generate(seed, n_devices=40)
    cohort = derive_cohort(devices_from_rows(devices_rows),
                           events_from_rows(events_rows), cutoff, obs_end)
    estimates = estimate_groups(cohort.intervals)
    reference = reference_estimate(cohort.intervals)
    problems = checks.check_all(estimates)
    problems += checks.compare_with_reference(estimates, reference)
    assert problems == [], f"seed={seed}: {problems}"
