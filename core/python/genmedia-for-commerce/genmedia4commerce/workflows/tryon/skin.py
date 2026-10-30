# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The wearer's skin shade and brightness, face included, in the drawing
prompt of both routes (``prompt``, ``outfit_prompt``): the system sentence,
the identity rule and what a product photo on another person gives (or on
a mannequin: a worn photo whose person became one, ``worn``).
"""

__all__ = [
    "CROP",
    "CROP_MANNEQUIN",
    "IDENTITY",
    "ONLY",
    "ONLY_MANNEQUIN",
    "SKIN_STATE",
]

# In the system text: {skin_from} names the wearer's images, {face} the
# face's, {others} the images of other people.
SKIN_STATE = (
    "In your generation description, you MUST explicitly state the exact "
    "skin tone, shade, undertone, and brightness/lightness of the wearer in "
    "{skin_from} (for example: fair, light tan, medium warm golden-brown, or "
    "deep dark brown) and state that all of the wearer's visible skin, the "
    "face included, keeps the exact skin brightness and warm/cool shade of "
    "{face} without darkening or lightening toward the skin tone of the "
    "people shown in {others}."
)
# The front's identity rule: {face} is the face's source, {body} the body's.
IDENTITY = (
    "Identity. The face matches {face} exactly: features, skin texture, "
    "skin shade and brightness. Hairstyle, hair "
    "colour and length stay as in {body}. Do not beautify, age, slim or "
    "reshape the wearer. Keep the head angle, gaze and facial expression of "
    "{face} exactly: a face in profile stays in profile, a three-quarter face "
    "stays three-quarter, a smile stays a smile; never turn the face towards "
    "the camera or copy the head angle of a catalogue model."
)
# A product photo on a person ({what}: the product's name).
ONLY = "ONLY the {what} from it, never that person's face, skin or body"
ONLY_MANNEQUIN = "ONLY the {what} from it"  # on a mannequin
# A REFERENCE CROP, after the products it shows: cut from a photo of
# another person, or of a mannequin.
CROP = (
    " as worn by another person, cropped from a photo of them; use it only "
    "for how it is worn and for its details; never copy that person's body, "
    "face or skin tone."
)
CROP_MANNEQUIN = (
    " as worn on a mannequin, cropped from a photo of it; use it only for "
    "how it is worn and for its details."
)
