from app.chunking import ParsedUnit, chunk_units, estimate_tokens


def test_estimate_tokens_handles_mixed_language() -> None:
    assert estimate_tokens("订单 API status code") >= 5


def test_chunking_preserves_source_location() -> None:
    chunks = chunk_units(
        [
            ParsedUnit(
                text="订单状态 | Order Status\n已批准 | APPROVED",
                heading_path="接口设计 > 状态",
                sheet_name="Status",
                cell_range="A1:B2",
                is_table=True,
            )
        ],
        max_tokens=650,
    )
    assert len(chunks) == 1
    assert chunks[0].sheet_name == "Status"
    assert chunks[0].cell_range == "A1:B2"
    assert chunks[0].content_hash


def test_long_text_is_split_below_limit() -> None:
    text = "。".join(["这是一个用于验证切块长度的中文句子" for _ in range(120)])
    chunks = chunk_units([ParsedUnit(text=text)], max_tokens=100, overlap_tokens=10)
    assert len(chunks) > 1
    assert all(chunk.token_count <= 100 for chunk in chunks)

