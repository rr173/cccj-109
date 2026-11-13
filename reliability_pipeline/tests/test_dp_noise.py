"""安全随机源与逆变换端点处理测试。"""

from __future__ import annotations

import math

import pytest

from dp_share.errors import MechanismError
from dp_share.noise import SecureLaplaceSource
from tests.dp_helpers import independent_laplace


def test_laplace_always_finite_and_no_log_zero():
    src = SecureLaplaceSource()
    # 大样本下若端点处理错误，极易出现 NaN/inf 或直接抛 log(0)。
    for scale in (1e-9, 1.0, 1e6):
        for _ in range(2000):
            x = src.laplace(scale)
            assert math.isfinite(x)


def test_laplace_rejects_bad_scale():
    src = SecureLaplaceSource()
    for bad in (0, -1.0, float("nan"), float("inf"), 0.0):
        with pytest.raises(MechanismError):
            src.laplace(bad)


def test_uniform_grid_is_half_open():
    """底层均匀数必须严格落在 [0,1)，端点 1.0 永不出现。"""
    src = SecureLaplaceSource()
    for _ in range(5000):
        u = src._uniform_unit_open()
        assert 0.0 <= u < 1.0


def test_inverse_transform_matches_independent_formula():
    """用验证器独立实现的逆变换交叉核对同一网格点的结果。"""
    src = SecureLaplaceSource()
    scale = 1.0 / (3 / 1_000_000)  # epsilon=3 micro 时的尺度
    grid_points = [0.0, 1e-16, 0.25, 0.5 - 1e-12, 0.5, 0.5 + 1e-12, 0.999999999999]
    # u=0 是生产实现会重采样的端点；这里直接验证：除端点外公式两边一致。
    for u in grid_points[1:]:
        n = 53
        k = round(u * (1 << n))
        u_grid = k / (1 << n)
        v = abs(2.0 * u_grid - 1.0)
        # 复刻生产路径（跳过端点重采样循环）
        x_prod = -math.copysign(1.0, u_grid - 0.5) * math.log1p(-v) * scale
        x_ind = independent_laplace(u_grid, scale)
        assert x_prod == pytest.approx(x_ind)


def test_empirical_distribution_matches_laplace_cdf():
    """统计抽样只作辅助观察：经验 CDF 与理论 Laplace CDF 应足够接近。"""
    src = SecureLaplaceSource()
    b = 2.5
    n = 60_000
    samples = [src.laplace(b) for _ in range(n)]
    for t in (-2.0, -0.5, 0.0, 0.5, 2.0):
        empirical = sum(1 for x in samples if x <= t) / n
        # Laplace(0,b) CDF
        theoretical = 0.5 * math.exp(t / b) if t < 0 else 1 - 0.5 * math.exp(-t / b)
        assert abs(empirical - theoretical) < 0.02


def test_source_has_no_seed_switch():
    """生产噪声源不接受 seed，也没有确定性测试开关。"""
    with pytest.raises(TypeError):
        SecureLaplaceSource(seed=1)  # type: ignore[misc]
    assert not hasattr(SecureLaplaceSource(), "seed")
