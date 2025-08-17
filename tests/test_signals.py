import pandas as pd
from src.signals import momentum_score, inverse_vol_weights

def test_momentum_score_basic():
    idx = pd.date_range("2024-01-01", periods=10, freq="H")
    df = pd.DataFrame({
        "A": range(10, 20),
        "B": range(20, 30),
    }, index=idx)
    sc = momentum_score(df, [1, 3], [1.0, 1.0])
    assert sc.index.tolist() == ["A", "B"]
    assert all(~sc.isna())

def test_inverse_vol_weights_sum_to_one():
    idx = pd.date_range("2024-01-01", periods=60, freq="H")
    df = pd.DataFrame({
        "A": (100 + pd.Series(range(60))).values,
        "B": (200 + pd.Series(range(60))*2).values,
    }, index=idx)
    iv = inverse_vol_weights(df, 30)
    assert abs(iv.sum() - 1.0) < 1e-8
