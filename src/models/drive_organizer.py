from pydantic import BaseModel, Field, field_validator


class IntegrationCreateRequest(BaseModel):
    provider: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    base_url: str = Field(..., min_length=1)

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        if value != "google_drive":
            raise ValueError("provider must be google_drive")
        return value

    @field_validator("name", "base_url")
    @classmethod
    def strip_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value
