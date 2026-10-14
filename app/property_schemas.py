
from typing import Literal

from pydantic import BaseModel, Field


class PropertyCreate(BaseModel):
    title: str = Field(min_length=5, max_length=120)
    description: str = Field(min_length=20, max_length=3000)

    property_type: Literal["Room", "Flat", "House", "PG"]
    rent: int = Field(gt=0, le=10000000)
    deposit: int = Field(ge=0, le=100000000)

    area: str = Field(min_length=2, max_length=100)
    address: str = Field(min_length=5, max_length=300)

    bedrooms: int = Field(ge=0, le=50)
    bathrooms: int = Field(ge=0, le=50)

    furnishing: Literal[
        "Furnished",
        "Semi-furnished",
        "Unfurnished",
    ]

    tenant_preference: Literal[
        "Students",
        "Bachelors",
        "Family",
        "Anyone",
    ]

    image_urls: list[str] = Field(default_factory=list, max_length=10)
