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

"""Video prompts of the 360 spin: the tuned shoe prompt and the two half turns."""

from collections.abc import Sequence

__all__ = ["SHOE_SPIN_PROMPT", "TURN_DURATION", "orbit_turns", "shoe_prompt"]

# Turntable-spin prompt for shoes (ported verbatim from the omni shoe pipeline).
SHOE_SPIN_PROMPT = """**[Subject]:** A shoe on the same background it has in the provided reference images — keep that background exactly as-is, unchanged. The exact shoe shown in the provided reference images — preserve its shape, color, materials, branding, laces, and sole exactly.

**[Starting Frame]:** The shoe is standing upright on the ground, sole down, viewed from its SIDE profile. The toe of the shoe points to the RIGHT edge of the screen. The heel points to the LEFT edge of the screen. The camera is roughly at shoe-eye level, straight-on side view. The shoe is stationary at frame 0.

**[Rotation — TURNTABLE spin, NOT a cartwheel]:** The shoe spins in place around its own VERTICAL axis (imagine a pin going straight down through the top of the shoe into the floor, and the shoe rotates around that pin — like a product on a rotating turntable / lazy Susan). The shoe stays upright the ENTIRE time. The sole stays down. The top of the shoe stays up. The toe NEVER points down. The toe NEVER points up. The shoe does NOT cartwheel, tumble, flip, or lay on its side.

**[Toe path around the shoe as it turns]:** Follow the toe throughout the clip. Because this is a horizontal turntable spin, the toe traces a horizontal circle around the shoe (not a vertical circle on screen). The toe passes through these positions in this EXACT order:
  1. Toe pointing to the viewer's RIGHT — side profile, toe visible on screen-right (start)
  2. Toe pointing TOWARD the camera (out of the screen) — you now see the FRONT of the shoe head-on (at about 2 seconds)
  3. Toe pointing to the viewer's LEFT — opposite side profile, toe on screen-left (at about 4 seconds)
  4. Toe pointing AWAY from the camera (into the screen) — you now see the BACK / heel of the shoe (at about 6 seconds)
  5. Toe pointing to the viewer's RIGHT again — identical to start (at about 8.5 seconds), then it keeps turning the same way until the end
From directly above, the toe traces a CIRCLE going CLOCKWISE (right → toward-camera → left → away-from-camera → right). Do NOT go in the opposite order — the toe must NEVER go right → away → left → toward → right (that is the wrong direction). On screen the toe appears to shrink into the picture and grow back out, never moving up or down. The rotation NEVER reverses, NEVER changes direction.

**[Motion Quality]:** Perfectly smooth, steady, constant angular speed from frame 0 to the final frame, fast enough to complete the full turn by about 8.5 seconds. No acceleration, no deceleration, no pauses, no direction changes. One single continuous rotation.

**[Full Turn]:** The shoe comes back EXACTLY to the FIRST frame's pose — same pose, same lighting, same framing, side profile with toe pointing RIGHT — at about 8.5 seconds, completing one full turntable rotation, and it keeps turning at the same speed after that. Never stop before the full rotation is complete.

**[Camera]:** The camera is completely static — fixed position, fixed framing, fixed zoom. The camera does not move at all. Only the shoe moves.

**[Edit/Continuity]:** The clip is ONE single continuous take from start to finish. NO cuts, NO jump cuts, NO transitions, NO crossfades, NO scene changes, NO splices. The shoe never disappears and reappears. A single uninterrupted shot the whole time.

**[Fidelity to Reference]:** Preserve the shoe EXACTLY as shown in the reference images, including any imperfections — dirt, scuffs, scratches, stains, wear marks, creases, discoloration, or any other defects. Do NOT clean, retouch, restore, or beautify the shoe. Show it in its real current condition.

**[No Inferred / Invented Branding]:** Render ONLY what is actually visible in the reference images. Do NOT infer, guess, imagine, or add any brand elements that are not clearly shown in the references — no logos, no wordmarks, no brand names, no monograms, no embossed marks, no printed text, no size/care labels, no sole markings, no hardware engravings. If a surface (e.g. the heel, tongue, insole, or sole) is not visible in any reference image, leave it PLAIN when it rotates into view — do NOT fabricate a logo, text, or pattern for it based on what you assume the brand "should" have. If the reference shows a logo or text, reproduce it EXACTLY as-is (same shape, letters, position); never sharpen it into different letters or a recognizable brand you infer. When unsure, render a blank/plain surface rather than an invented mark.

**[Hardware & Fine Details — Reproduce Precisely]:** Pay close, deliberate attention to every piece of hardware and fine detail on the shoe and reproduce each one EXACTLY as it appears in the reference images. This includes (whenever present): buckles, clasps, D-rings, charms, pendants, tassels, bit/chain trims, zippers and zip pulls, eyelets, lace hooks, studs, rivets, snaps, decorative metal plates, and stitching/piping. For each such element keep its precise SHAPE, TYPE, COUNT, SIZE, RELATIVE POSITION, and FINISH (metal color/tone) consistent with the reference — e.g. if the reference shows a specific arrangement of small charms, keep that same number and arrangement, do not merge them into a blob, swap their shapes, add extra ones, drop any, or turn a distinct shape into a generic disc. As the shoe rotates, these details must stay STABLE and IDENTICAL frame to frame (only their viewing angle changes); a piece of hardware must never morph, flicker, change count, or change shape during the spin. When a detailed element rotates partially out of view, show only the visible portion faithfully — never invent or complete it with a guessed shape.

**[Scene]:** Keep the background exactly as it appears in the reference images — do not change, replace, recolor, or restyle it. The only moving element is the shoe; the background stays unchanged throughout. {aspect_ratio} aspect ratio.

**[Audio]:** No audio. Completely silent. No music, no ambient sound, no sound effects, no voiceover, no narration. The output audio track is empty.

**[No On-Screen Text or Overlays]:** The video must contain ZERO text, captions, subtitles, watermarks, logos, brand marks, HUD elements, labels, callouts, arrows, dimensions, color chips, hex codes, or any other graphic overlay. The frame contains ONLY the shoe on its original background — nothing else, no writing of any kind. Do NOT render any technical specification from this prompt as visible text on the video. These instructions are for you (the generator) — they are NOT to appear anywhere in the output frames."""

# Any other product spins in two half turns in one interaction, the second
# extending the first (gen.generate_video ``extends``): each half has its own
# destination (the back, then the front), so the video never turns back over
# the side it has already shown, which a single full-turn request did in about
# a third of bag spins. Both turns attach the same references in the same
# order; <IMAGE_N> is the N-th of them. Timestamps open intervals, so each
# half reaches its destination before its last second.
TURN_DURATION = "4s"
_MUTE = "Audio: No sound, completely muted audio track."


def _closeup_lines(views: Sequence[str], closeups: Sequence) -> str:
    """One sentence per close-up reference (attached after the views)."""
    lines = []
    for k, c in enumerate(closeups, start=len(views)):
        reads = f", the lettering reading '{c.text}'" if c.text else ""
        lines.append(
            f" <IMAGE_{k}> is a close-up of the {c.where} (a detail reference, "
            "not a shot: the camera never zooms in on it or cuts to it); it "
            f"looks exactly like that{reads}."
        )
    return "".join(lines)


def _side(views: Sequence[str], closeups: Sequence, side: str) -> str:
    """ ", with the ... exactly as in <IMAGE_k>" for the close-ups on ``side``."""
    found = [
        f"the {c.where} exactly as in <IMAGE_{k}>"
        for k, c in enumerate(closeups, start=len(views))
        if c.side == side
    ]
    return f", with {' and '.join(found)}" if found else ""


def orbit_turns(
    description: str, views: Sequence[str], closeups: Sequence = ()
) -> tuple[str, str]:
    """The two half-turn prompts of a non-shoe spin.

    ``views``: the references' angles, in the order they are attached (the
    first is the starting view). ``closeups`` (``details.Detail``): close-up
    references attached after the views, each tied to the moment its side
    faces the camera.
    """
    images = ", ".join(f"<IMAGE_{i}>" for i in range(len(views)))
    back = f" matching <IMAGE_{views.index('back')}>" if "back" in views else ""
    back += _side(views, closeups, "back")
    front = _side(views, closeups, "front")
    product = (
        f"The product is {description}, floating still in the centre of an "
        "empty, seamless white space with soft, even studio lighting, with no "
        f"turntable, floor or shadow. It looks exactly like {images}: the same "
        "shape, colours, materials, logos, text and hardware; surfaces no "
        "image shows stay plain. A removable accessory (a detachable strap, a "
        "charm, a tag) that appears in only some of the images is left off."
        + _closeup_lines(views, closeups)
    )
    first = (
        f"[duration={TURN_DURATION}] Smooth continuous camera orbit halfway "
        f"around a stationary product (180 degrees total). {product}\n"
        "[00:00.000] The camera starts directly in front of the centred product "
        f"matching <IMAGE_0>{front}, then begins orbiting smoothly to the "
        f"right at a constant speed. {_MUTE}\n"
        "[00:01.500] The camera passes the ninety-degree side profile as it "
        f"continues orbiting in the same direction toward the back. {_MUTE}\n"
        f"[00:03.000] The camera arrives squarely at the back view{back} and "
        f"keeps a steady orbital momentum. {_MUTE}"
    )
    second = (
        f"[duration={TURN_DURATION}] Continue the smooth camera orbit in the "
        "exact same direction for the remaining 180 degrees, from the back view "
        "back to the front view.\n"
        "[00:00.000] Continuing seamlessly from the back view, the camera orbits "
        "past the opposite side profile in the same uninterrupted direction. "
        f"{_MUTE}\n"
        "[00:02.500] The camera completes the full 360-degree orbit, arriving "
        f"back at the exact front view matching <IMAGE_0>{front}, centred in "
        f"the frame. {_MUTE}"
    )
    return first, second


def _mirrored_note(positions: Sequence[int]) -> str:
    """The sentence naming the mirrored references (1-based positions)."""
    if len(positions) == 1:
        return (
            f"Reference image {positions[0]} is a generated mirror image of "
            "the other shoe of the pair: use it only for the shoe's shape, "
            "colour and materials; take text and logos only from the other "
            "references."
        )
    *rest, last = positions
    names = f"{', '.join(map(str, rest))} and {last}"
    return (
        f"Reference images {names} are generated mirror images of the other "
        "shoe of the pair: use them only for the shoe's shape, colour and "
        "materials; take text and logos only from the other references."
    )


def shoe_prompt(aspect_ratio: str, mirrored: Sequence[int] = ()) -> str:
    """The tuned shoe spin prompt for ``aspect_ratio``.

    ``mirrored``: the 1-based positions of the mirrored references in the
    images sent with it; one sentence is appended naming them (none when
    there are none, so the prompt stays the tuned one).
    """
    prompt = SHOE_SPIN_PROMPT.replace("{aspect_ratio}", aspect_ratio)
    if not mirrored:
        return prompt
    return f"{prompt}\n\n{_mirrored_note(mirrored)}"
