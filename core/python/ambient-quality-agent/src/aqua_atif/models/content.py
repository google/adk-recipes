# SPDX-License-Identifier: Apache-2.0
#
# Copied from Harbor (https://github.com/harbor-framework/harbor), release
# v0.25.0, file src/harbor/models/trajectories/content.py. Copyright the Harbor
# authors, licensed under the Apache License, Version 2.0; see LICENSE in this
# directory. Modified for AQuA: imports point at `aqua_atif.models`, and each
# other change is marked with an "AQuA change" comment (see `__init__.py`).

"""Content models for multimodal ATIF trajectories.

Added in ATIF-v1.6 to support multimodal content (images) in trajectories.
Extended in ATIF-v1.8 with audio.
"""

from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

# Canonical audio MIME types, chosen to cover what multimodal and transcription
# APIs actually accept: Gemini audio understanding (wav, mp3, aiff, aac, ogg,
# flac), OpenAI transcription (flac, mp3, mp4, mpeg, mpga, m4a, ogg, wav, webm),
# and `audio/webm`, which is what a browser's MediaRecorder produces and so what
# human-recorded task data usually arrives as.
AudioMediaType = Literal[
    "audio/wav",
    "audio/mpeg",
    "audio/mp4",
    "audio/aac",
    "audio/ogg",
    "audio/flac",
    "audio/webm",
    "audio/aiff",
]

# Aliases producers emit in place of the registered MIME type, normalized on the
# way in so consumers only ever see one spelling per format. `audio/mp3` is the
# important one: it is not a registered type, but the Gemini API documents and
# emits it, so a producer copying a provider response verbatim would otherwise
# fail validation.
_AUDIO_MEDIA_TYPE_ALIASES = {
    "audio/mp3": "audio/mpeg",
    "audio/mpga": "audio/mpeg",
    "audio/x-mpeg": "audio/mpeg",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/vnd.wave": "audio/wav",
    "audio/x-m4a": "audio/mp4",
    "audio/m4a": "audio/mp4",
    "audio/x-aac": "audio/aac",
    "audio/x-flac": "audio/flac",
    "audio/x-aiff": "audio/aiff",
}


# AQuA change 4: the image media types that ATIF defines. The fields below take
# any string, and `aqua_atif.validation` checks them against these lists.
ImageMediaType = Literal["image/jpeg", "image/png", "image/gif", "image/webp"]


class ImageSource(BaseModel):
    """Image source specification for images stored as files or at remote URLs."""

    # AQuA change 4: any string.
    media_type: str = Field(
        default=...,
        description="MIME type of the image",
    )
    path: str = Field(
        default=...,
        description=(
            "Location of the image. Can be a relative or absolute file path, or a URL."
        ),
    )

    # AQuA change 1: unknown fields are kept; `aqua_atif.validation` lists them.
    model_config = {"extra": "allow"}


class AudioSource(BaseModel):
    """Audio source specification for audio stored as files or at remote URLs.

    Added in ATIF-v1.8. Mirrors ``ImageSource`` so that consumers can treat any
    referenced media the same way, with one addition: ``duration_sec``. Duration
    is worth recording because several providers bill audio per second and
    dataset statistics need it, and unlike an image's dimensions it cannot be
    recovered without decoding the file.
    """

    # AQuA change 4: any string.
    media_type: str = Field(
        default=...,
        description="MIME type of the audio",
    )
    path: str = Field(
        default=...,
        description=(
            "Location of the audio. Can be a relative or absolute file path, or a URL."
        ),
    )
    duration_sec: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Optional duration of the audio in seconds. Useful for cost "
            "accounting and dataset statistics; omit when unknown."
        ),
    )

    # AQuA change 1: unknown fields are kept; `aqua_atif.validation` lists them.
    model_config = {"extra": "allow"}

    @field_validator("media_type", mode="before")
    @classmethod
    def normalize_media_type(cls, value: Any) -> Any:
        """Accept common non-registered spellings, storing the canonical one."""
        if isinstance(value, str):
            normalized = value.strip().lower()
            return _AUDIO_MEDIA_TYPE_ALIASES.get(normalized, normalized)
        return value


class ContentPart(BaseModel):
    """A single content part within a multimodal message.

    Used when a message or observation contains mixed content types (text,
    images, or audio). For text-only content, a plain string can still be used
    instead of a ContentPart array.
    """

    # AQuA change 4: any string, so a part type from a newer version is kept.
    type: str = Field(
        default=...,
        description="The type of content",
    )
    text: str | None = Field(
        default=None,
        description="Text content. Required when type='text'.",
    )
    # AQuA change 4: a part of an unknown type keeps its source as written.
    source: ImageSource | AudioSource | dict[str, Any] | None = Field(
        default=None,
        description=(
            "Media source (file reference). Required when type='image' or "
            "type='audio', and must match that type."
        ),
    )

    # AQuA change 1: unknown fields are kept; `aqua_atif.validation` lists them.
    model_config = {"extra": "allow"}

    @field_validator("source", mode="before")
    @classmethod
    def select_source_model(cls, value: Any, info: ValidationInfo) -> Any:
        """AQuA change 4: choose the source model from the part's type.

        With open media type lists, an image source and an audio source have the
        same shape, so the union cannot tell them apart by content. Validating
        here, not on the whole part, keeps `source` in error locations.
        """
        if not isinstance(value, dict):
            return value
        if info.data.get("type") == "image":
            return ImageSource.model_validate(value)
        if info.data.get("type") == "audio":
            return AudioSource.model_validate(value)
        return value

    @model_validator(mode="after")
    def validate_content_type(self) -> "ContentPart":
        """Validate that the correct fields are present for each content type."""
        if self.type == "text":
            if self.text is None:
                raise ValueError("'text' field is required when type='text'")
            if self.source is not None:
                raise ValueError(
                    "'source' field is not allowed when type='text'"
                )
            return self
        # AQuA change 4: a part of an unknown type is kept as written.
        if self.type not in ("image", "audio"):
            return self

        # Media parts: a source is required, text is not allowed, and the source
        # must be of the kind the `type` declares. Without the last check a
        # `source` union would happily accept an ImageSource under
        # type='audio' (and vice versa), silently mislabelling the content.
        if self.source is None:
            raise ValueError(
                f"'source' field is required when type='{self.type}'"
            )
        if self.text is not None:
            raise ValueError(
                f"'text' field is not allowed when type='{self.type}'"
            )

        expected = ImageSource if self.type == "image" else AudioSource
        if not isinstance(self.source, expected):
            raise ValueError(
                f"type='{self.type}' requires a {expected.__name__}, but the "
                # AQuA change 4: a source of an unknown part type is a dict.
                f"source's media_type is {getattr(self.source, 'media_type', None)!r}"
            )
        return self
