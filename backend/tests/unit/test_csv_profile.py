from relay_core.connectors.builtin.csv_profile import infer_capabilities, profile_csv, read_rows

_CSV = b"account_name,month,active_users,api_calls\nAcme,2026-08,58,1044\nGlobex,2026-08,88,1584\n"


def test_profile_csv_reads_columns_sample_and_row_count() -> None:
    profile = profile_csv(_CSV)
    assert profile.columns == ["account_name", "month", "active_users", "api_calls"]
    assert profile.row_count == 2
    assert profile.sample_rows[0]["account_name"] == "Acme"


def test_infer_capabilities_matches_usage_keywords() -> None:
    # "account_name" also matches the customer.read keyword "account" — both are legitimately
    # inferred from this header row, not just usage.read.
    inferred = infer_capabilities(["account_name", "month", "active_users", "api_calls"])
    assert set(inferred) == {"usage.read", "customer.read"}


def test_infer_capabilities_matches_multiple_capabilities() -> None:
    inferred = infer_capabilities(["customer_name", "renewal_date", "active_users"])
    assert set(inferred) == {"subscription.read", "usage.read", "customer.read"}


def test_infer_capabilities_empty_when_nothing_matches() -> None:
    assert infer_capabilities(["foo", "bar"]) == []


def test_read_rows_respects_the_limit_and_reports_truncation() -> None:
    rows, truncated = read_rows(_CSV, limit=1)
    assert len(rows) == 1
    assert truncated is True

    rows, truncated = read_rows(_CSV, limit=10)
    assert len(rows) == 2
    assert truncated is False
