from pydantic import BaseModel, Field


class RawTransaction(BaseModel):
  row: dict[str, str] = Field(
    default_factory=dict,
    description="Raw key-value pairs from one table row",
  )


class ParsedTable(BaseModel):
  headers: list[str] = Field(default_factory=list)
  rows: list[dict[str, str]] = Field(default_factory=list)


class ParseResponse(BaseModel):
  filename: str
  file_type: str = Field(description="pdf or csv")
  tables: list[ParsedTable] = Field(
    default_factory=list,
    description="Extracted tables with headers and rows",
  )
  markdown: str = Field(
    default="",
    description="Full document text as markdown",
  )
  row_count: int = Field(
    default=0,
    description="Total number of data rows across all tables",
  )


class HealthResponse(BaseModel):
  status: str = "ok"
  docling_ready: bool = False
