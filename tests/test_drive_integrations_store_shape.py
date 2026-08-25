import pytest
from pydantic import ValidationError

from src.models.drive_organizer import IntegrationCreateRequest


def test_create_google_drive_integration_rejects_blank_name():
    with pytest.raises(ValidationError):
        IntegrationCreateRequest(
            provider="google_drive",
            name="",
            base_url="https://www.googleapis.com",
        )


def test_create_google_drive_integration_rejects_blank_base_url():
    with pytest.raises(ValidationError):
        IntegrationCreateRequest(
            provider="google_drive",
            name="Drive",
            base_url="",
        )
