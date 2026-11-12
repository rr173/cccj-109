"""pytest 封装：金标准核对（等价于 `verify-gold` 子命令）。"""

from pipeline.cli import _parse_date, _run_pipeline
from pipeline.gold_data import EXPECTED, write_gold_csvs
from pipeline.verify import verify_gold_run


def test_gold_runs_match_hand_computed(tmp_path):
    data_dir = tmp_path / "gold"
    write_gold_csvs(data_dir)
    for run_id, exp in EXPECTED.items():
        _, _, cohort, estimates, problems = _run_pipeline(
            str(data_dir),
            _parse_date(exp["knowledge_cutoff"]),
            _parse_date(exp["observation_end"]),
        )
        problems += verify_gold_run(run_id, cohort, estimates)
        assert problems == [], f"{run_id}: {problems}"
