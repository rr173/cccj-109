"""Laplace 噪声源。

生产实现 :class:`SecureLaplaceSource` 只使用操作系统安全随机源
（:mod:`secrets`，对应 ``getrandom(2)`` / ``/dev/urandom`` 之类的内核 CSPRNG），
不接受研究方指定 seed，也不存在确定性测试开关接到发布命令上。

逆变换采样
==========
对 ``U ~ Uniform(0,1)``，``X = b * sign(U-0.5) * ln(1 - 2*|U-0.5|)``
服从 ``Laplace(0, b)``（等价的 ``-b*sgn(V)*ln(1-|V|)`` 形式）。

端点处理（工程上必须显式处理，不能碰运气）：
- 不直接用 ``random.random()`` 之类可能返回 0.0 的接口再取 ``ln``；
- 先在整数集合 ``[0, 2^53)`` 上用 CSPRNG 做拒绝采样得到严格落在
  ``[0, 2^53)`` 的整数，再除以 ``2^53``，故 ``u`` 严格属于 ``[0.0, 1.0)``；
- ``v = |2u - 1|``：``u=0`` 时 ``v=1`` 会导致 ``ln(1-v)=ln(0)``，因此
  对 ``v >= 1``（只有 ``u=0`` 这一个点）重采样；
- 于是 ``1 - v`` 严格为正，不可能出现 ``log(0)``、NaN 或无穷大；
  尺度 ``b`` 有限且为正时输出也必为有限值。

注意：这里的浮点实现是**工程近似**——实数 Laplace 机制的差分隐私定义在
IEEE-754 有限精度舍入下不自动继承，本实现不宣称形式化的有限精度隐私证明。
"""

from __future__ import annotations

import math
import secrets
from typing import Protocol, runtime_checkable

from .errors import MechanismError

# 2^53：双精度浮点数能精确表示的整数上界，保证除法后是 [0,1) 上的均匀网格。
_GRID = 1 << 53


@runtime_checkable
class NoiseSource(Protocol):
    """噪声源协议。测试可在引擎内部注入实现此协议的确定性源。"""

    def laplace(self, scale: float) -> float:
        """返回一个 Laplace(0, scale) 抽样。"""
        raise NotImplementedError


class SecureLaplaceSource:
    """基于 :mod:`secrets` 的生产噪声源，无 seed、无可切换的确定性模式。"""

    def _uniform_unit_open(self) -> float:
        """返回严格满足 0.0 <= u < 1.0 的均匀网格点。

        ``secrets.randbelow`` 在 ``[0, 2^53)`` 内均匀取整数；理论上不会越界，
        显式范围断言是为了在底层实现异常时 fail-closed，而不是产出坏噪声。
        """
        n = secrets.randbelow(_GRID)
        if not 0 <= n < _GRID:  # pragma: no cover - 防御性断言
            raise MechanismError("安全随机源返回了越界整数")
        return n / _GRID

    def laplace(self, scale: float) -> float:
        if not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0:
            raise MechanismError(f"Laplace 尺度必须是正有限值，收到: {scale!r}")
        # 显式处理逆变换端点：只有 u == 0 会让 v == 1，重采样避开 log(0)。
        while True:
            u = self._uniform_unit_open()
            v = abs(2.0 * u - 1.0)
            if v < 1.0:
                break
        x = -math.copysign(1.0, u - 0.5) * math.log1p(-v)
        noise = scale * x
        if not math.isfinite(noise):  # pragma: no cover - 端点已处理，防御性
            raise MechanismError("Laplace 抽样产生了非有限值")
        return noise
