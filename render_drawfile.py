#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# render_drawfile.py
#
# The Python script in this file is part of drawfile-render: a tool for
# rendering Acorn !Draw files to PNG and SVG.
#
# Copyright (C) 2010-2026 Dominic Ford <https://dcford.org.uk/>
#
# This code is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 3 of the License, or (at your option) any later
# version.
#
# You should have received a copy of the GNU General Public License along with
# this file; if not, write to the Free Software Foundation, Inc., 51 Franklin
# Street, Fifth Floor, Boston, MA  02110-1301, USA

# ----------------------------------------------------------------------------

"""
This Python script renders Acorn Draw files in a variety of formats.

References:
    http://justsolve.archiveteam.org/wiki/Acorn_Draw
    https://www.riscosopen.org/wiki/documentation/show/File%20formats:%20DrawFile
    http://www.riscos.com/support/users/grapharm/chap17.htm
    http://www.wss.co.uk/pinknoise/Docs/Arc/Draw/DrawFiles.html
"""

import argparse
import ctypes
import ctypes.util
import io
import glob
import logging
import math
import os
import sys

from typing import Dict, List, Optional, Sequence

from graphics_context import GraphicsContext, GraphicsPage
import spritefile, spr2img
import temporary_directory


def _register_riscos_fonts() -> bool:
    """
    Register the bundled RISC OS font directory with fontconfig so that Cairo
    can resolve the original RISC OS font family names (Homerton, Trinity,
    Corpus, etc.) directly.

    :return:
        True if the fonts were registered successfully, False otherwise.
    """
    font_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "riscos-free-fonts", "Fonts")
    if not os.path.isdir(font_dir):
        logging.warning("RISC OS font directory not found: %s", font_dir)
        return False

    fc_lib_name = ctypes.util.find_library("fontconfig")
    if fc_lib_name is None:
        logging.warning("fontconfig library not found, cannot register RISC OS fonts")
        return False

    try:
        fc = ctypes.cdll.LoadLibrary(fc_lib_name)
        fc.FcConfigAppFontAddDir.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        fc.FcConfigAppFontAddDir.restype = ctypes.c_int
        result = fc.FcConfigAppFontAddDir(None, font_dir.encode("utf-8"))
        if result:
            logging.debug("Registered RISC OS fonts from %s", font_dir)
            return True
        else:
            logging.warning("FcConfigAppFontAddDir failed for %s", font_dir)
            return False
    except OSError as e:
        logging.warning("Failed to load fontconfig: %s", e)
        return False


# Register bundled RISC OS fonts at import time
_RISCOS_FONTS_AVAILABLE = _register_riscos_fonts()

# Translation table for RISC OS Acorn Latin1 to Unicode.
# ISO 8859-1 maps bytes 0x80-0x9F to C1 control characters, but RISC OS
# defines printable characters in this range. We decode as iso-8859-1 (which
# is a lossless 1:1 mapping) then translate the 0x80-0x9F codepoints to the
# correct Unicode characters.
_RISCOS_LATIN1_TRANSLATION = str.maketrans({
    0x80: '\u20ac',  # Euro sign
    0x81: '\u0174',  # Latin capital letter W with circumflex
    0x82: '\u0175',  # Latin small letter w with circumflex
    0x83: '\ufffd',  # RISC OS resize window icon (no Unicode equivalent)
    0x84: '\ufffd',  # RISC OS close window icon (no Unicode equivalent)
    0x85: '\u0176',  # Latin capital letter Y with circumflex
    0x86: '\u0177',  # Latin small letter y with circumflex
    0x87: '\ufffd',  # RISC OS special character (no Unicode equivalent)
    0x88: '\u21e6',  # Leftwards white arrow
    0x89: '\u21e8',  # Rightwards white arrow
    0x8a: '\u21e9',  # Downwards white arrow
    0x8b: '\u21e7',  # Upwards white arrow
    0x8c: '\u2026',  # Horizontal ellipsis
    0x8d: '\u2122',  # Trade mark sign
    0x8e: '\u2030',  # Per mille sign
    0x8f: '\u2022',  # Bullet
    0x90: '\u2018',  # Left single quotation mark
    0x91: '\u2019',  # Right single quotation mark
    0x92: '\u2039',  # Single left-pointing angle quotation mark
    0x93: '\u203a',  # Single right-pointing angle quotation mark
    0x94: '\u201c',  # Left double quotation mark
    0x95: '\u201d',  # Right double quotation mark
    0x96: '\u201e',  # Double low-9 quotation mark
    0x97: '\u2013',  # En dash
    0x98: '\u2014',  # Em dash
    0x99: '\u2212',  # Minus sign
    0x9a: '\u0152',  # Latin capital ligature OE
    0x9b: '\u0153',  # Latin small ligature oe
    0x9c: '\u2020',  # Dagger
    0x9d: '\u2021',  # Double dagger
    0x9e: '\ufb01',  # Latin small ligature fi
    0x9f: '\ufb02',  # Latin small ligature fl
})


def bytes_to_uint(size: int, byte_array: bytes, position: int) -> int:
    """
    Convert an array of bytes into an unsigned integer of arbitrary byte width.

    :param size:
        The number of bytes in the unsigned integer.
    :param byte_array:
        The input array of bytes.
    :param position:
        The position of the start of the integer
    :return:
        Integer value
    """
    out = 0
    for index in range(size):
        out = out | (byte_array[position + index] << (index * 8))
    return out


def bytes_to_int(size: int, byte_array: bytes, position: int) -> int:
    """
    Convert an array of bytes into a signed integer of arbitrary byte width.
    Identical to bytes_to_uint but sign-extends the result.
    """
    out = bytes_to_uint(size=size, byte_array=byte_array, position=position)
    sign_bit = 1 << (size * 8 - 1)
    if out & sign_bit:
        out -= (sign_bit << 1)
    return out


def decode_riscos_string(data: bytes) -> str:
    """
    Decode a byte string from the RISC OS Acorn Latin1 character set to Unicode.

    :param data:
        Raw bytes in RISC OS encoding
    :return:
        Unicode string
    """
    return data.decode(encoding='iso-8859-1').translate(_RISCOS_LATIN1_TRANSLATION)


def colour_dict_from_int(uint: int) -> dict:
    """
    Fetch an RGB colour from a 32-bit int.

    :param uint:
        Integer colour specification
    :return:
        Dictionary describing the colour
    """

    return {
        "r": (uint & 0x0000FF00) >> 8,
        "g": (uint & 0x00FF0000) >> 16,
        "b": (uint & 0xFF000000) >> 24,
        "transparent": (uint & 0x000000FF) == 0xFF,
    }


def context_colour_from_int(uint: int) -> Sequence[float]:
    """
    Fetch a Cairo RGBA colour from a 32-bit int.

    :param uint:
        Integer colour specification
    :return:
        Sequence of RGBA components, in the range 0-1
    """

    colour_dict: dict = colour_dict_from_int(uint=uint)

    return [colour_dict["r"] / 255., colour_dict["g"] / 255., colour_dict["b"] / 255.,
            0. if colour_dict["transparent"] else 1.]


def _collect_open_subpaths(path_elements: List[Dict]) -> List[Dict]:
    """
    Walk a list of path elements and return endpoint/direction info for each open subpath.

    Closed subpaths (those containing a CLOSE element) are excluded because they have
    no free endpoints and thus need no line caps.

    Each returned dict has:
        start_x, start_y : first MOVE point (Draw units)
        start_dx, start_dy : direction FROM start toward interior (Draw units, unnormalised)
        end_x, end_y : last drawn point (Draw units)
        end_dx, end_dy : direction FROM interior TOWARD end (Draw units, unnormalised)
    """
    result: List[Dict] = []

    cur_x: float = 0.0
    cur_y: float = 0.0
    sub_start_x: float = 0.0
    sub_start_y: float = 0.0
    start_dx: float = 0.0
    start_dy: float = 0.0
    end_dx: float = 0.0
    end_dy: float = 0.0
    have_start_dir: bool = False
    have_end: bool = False
    in_open_subpath: bool = False

    def push_subpath() -> None:
        nonlocal in_open_subpath, have_start_dir, have_end
        if in_open_subpath and have_start_dir and have_end:
            result.append({
                'start_x': sub_start_x, 'start_y': sub_start_y,
                'start_dx': start_dx, 'start_dy': start_dy,
                'end_x': cur_x, 'end_y': cur_y,
                'end_dx': end_dx, 'end_dy': end_dy,
            })
        in_open_subpath = False
        have_start_dir = False
        have_end = False

    for elem in path_elements:
        t = elem['type']
        if t == 'END':
            push_subpath()
            break
        elif t == 'MOVE':
            push_subpath()
            sub_start_x = float(elem['x'])
            sub_start_y = float(elem['y'])
            cur_x, cur_y = sub_start_x, sub_start_y
            in_open_subpath = True
        elif t == 'CLOSE':
            in_open_subpath = False
            have_start_dir = False
            have_end = False
            cur_x, cur_y = sub_start_x, sub_start_y
        elif t == 'LINE':
            dx = float(elem['x']) - cur_x
            dy = float(elem['y']) - cur_y
            if not have_start_dir:
                start_dx, start_dy = dx, dy
                have_start_dir = True
            end_dx, end_dy = dx, dy
            cur_x, cur_y = float(elem['x']), float(elem['y'])
            have_end = True
        elif t == 'BEZIER':
            x0, y0 = float(elem['x0']), float(elem['y0'])
            x1, y1 = float(elem['x1']), float(elem['y1'])
            x2, y2 = float(elem['x2']), float(elem['y2'])
            if not have_start_dir:
                # Start tangent: current point toward first control point
                dx0, dy0 = x0 - cur_x, y0 - cur_y
                if abs(dx0) < 1e-6 and abs(dy0) < 1e-6:
                    dx0, dy0 = x1 - cur_x, y1 - cur_y
                if abs(dx0) < 1e-6 and abs(dy0) < 1e-6:
                    dx0, dy0 = x2 - cur_x, y2 - cur_y
                start_dx, start_dy = dx0, dy0
                have_start_dir = True
            # End tangent: second control point toward endpoint
            edx, edy = x2 - x1, y2 - y1
            if abs(edx) < 1e-6 and abs(edy) < 1e-6:
                edx, edy = x2 - x0, y2 - y0
            if abs(edx) < 1e-6 and abs(edy) < 1e-6:
                edx, edy = x2 - cur_x, y2 - cur_y
            end_dx, end_dy = edx, edy
            cur_x, cur_y = x2, y2
            have_end = True

    return result


def _draw_triangle_cap(context: 'GraphicsContext', apex_x: float, apex_y: float,
                       dir_x: float, dir_y: float,
                       cap_length_m: float, half_cap_width_m: float,
                       color: Sequence[float]) -> None:
    """
    Draw a filled triangular arrowhead cap as a standalone path.

    The apex (tip) is at (apex_x, apex_y). dir_x/dir_y is the unit-vector pointing
    FROM the apex outward (away from the path interior); the triangle base sits
    cap_length_m behind the apex (toward the path interior).
    All coordinates are in metres (Cairo space).
    """
    length = math.sqrt(dir_x * dir_x + dir_y * dir_y)
    if length < 1e-12:
        return
    dx, dy = dir_x / length, dir_y / length
    px, py = -dy, dx  # perpendicular unit vector
    base_cx = apex_x - dx * cap_length_m
    base_cy = apex_y - dy * cap_length_m
    context.begin_path()
    context.move_to(x=apex_x, y=apex_y)
    context.line_to(x=base_cx + px * half_cap_width_m, y=base_cy + py * half_cap_width_m)
    context.line_to(x=base_cx - px * half_cap_width_m, y=base_cy - py * half_cap_width_m)
    context.close_path()
    context.fill(color=color)


class DrawFileRender:
    # Draw files measure positions in units of 1/(180*256) inches
    pixel: float = 5.51215278e-07  # metres

    # Margin to allow around the image area / metres
    margin: float = 0.005

    # Draw file object types, as documented in the RISC OS manual
    object_types: Dict[int, dict] = {
        0: {
            "name": "Font table",
            "bbox": False,
            "bbox_include_in_render": False
        },
        1: {
            "name": "Text object",
            "bbox": True,
            "bbox_include_in_render": True,
            "fields": {
                "text_colour": [0, "uint", 4],
                "bg_colour_hint": [4, "uint", 4],
                "text_style": [8, "uint", 4],
                "x_size": [12, "uint", 4],
                "y_size": [16, "uint", 4],
                "x_baseline": [20, "uint", 4],
                "y_baseline": [24, "uint", 4],
                "text": [28, "str", 0]
            }
        },
        2: {
            "name": "Path object",
            "bbox": True,
            "bbox_include_in_render": True,
            "fields": {
                "fill_colour": [0, "uint", 4],
                "outline_colour": [4, "uint", 4],
                "outline_width": [8, "uint", 4],
                "path_style": [12, "uint", 4]
            }
        },
        5: {
            "name": "Sprite object",
            "bbox": True,
            "bbox_include_in_render": True
        },
        6: {
            "name": "Group object",
            "bbox": True,
            "bbox_include_in_render": False,
            "fields": {
                "name": [0, "str", 12]
            },
            "children_start": 12
        },
        7: {
            "name": "Tagged object",
            "bbox": True,
            "bbox_include_in_render": False,
            "fields": {
                "tag_id": [0, "uint", 4]
            },
            "children_start": 4
        },
        9: {
            "name": "Text area object",
            "bbox": True,
            "bbox_include_in_render": True,
            "children_start": 0,
            "fields_after_children": True,
            "fields": {
                "zero": [0, "uint", 4],
                "reserved_1": [4, "uint", 4],
                "reserved_2": [8, "uint", 8],
                "colour_foreground": [12, "uint", 4],
                "colour_background": [16, "uint", 4],
                "text": [20, "str", 0]
            }
        },
        10: {
            "name": "Text column object",
            "bbox": True,
            "bbox_include_in_render": True,
            "fields": {
                # "text": [0, "str", 0]
            }
        },
        11: {
            "name": "Options object",
            "bbox": True,
            "bbox_include_in_render": False,
            "fields": {
                "paper_size": [0, "uint", 4],
                "paper_limits": [4, "uint", 4],
                "grid_spacing": [8, "uint", 8],  # double-precision floating point
                "grid_division": [16, "uint", 4],
                "grid_type": [20, "uint", 4],
                "grid_auto_adjust": [24, "uint", 4],
                "grid_visible": [28, "uint", 4],
                "grid_units": [32, "uint", 4],
                "zoom_multiplier": [36, "uint", 4],
                "zoom_divider": [40, "uint", 4],
                "zoom_locking": [44, "uint", 4],
                "toolbox_presence": [48, "uint", 4],
                "initial_entry_mode": [52, "uint", 4],
                "undo_buffer_size": [56, "uint", 4]
            },
        },
        12: {
            "name": "Transformed text object",
            "bbox": True,
            "bbox_include_in_render": True,
            "fields": {
                "transformation_a": [0, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_b": [4, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_c": [8, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_d": [12, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_e": [16, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_f": [20, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "font_flags": [24, "uint", 4],
                "text_colour": [28, "uint", 4],
                "bg_colour_hint": [32, "uint", 4],
                "text_style": [36, "uint", 4],
                "x_size": [40, "uint", 4],
                "y_size": [44, "uint", 4],
                "x_baseline": [48, "uint", 4],
                "y_baseline": [52, "uint", 4],
                "text": [56, "str", 0]
            },
        },
        13: {
            "name": "Transformed sprite object",
            "bbox": True,
            "bbox_include_in_render": True,
            "fields": {
                "transformation_a": [0, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_b": [4, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_c": [8, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_d": [12, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_e": [16, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_f": [20, "int/65536", 4],  # fixed-point number &XXXX.XXXX
            },
        },
        16: {
            "name": "JPEG object",
            "bbox": True,
            "bbox_include_in_render": True
        },
        0x65: {
            "name": "[DrawPlus extension] DrawPlus settings",
            "bbox": False,
            "bbox_include_in_render": False
        },
        0x66: {
            "name": "[Vector extension] Static replicate",
            "bbox": False,
            "bbox_include_in_render": False
        },
        0x67: {
            "name": "[Vector extension] Dynamic replicate",
            "bbox": False,
            "bbox_include_in_render": False
        },
        0x69: {
            "name": "[Vector extension] Masked object",
            "bbox": False,
            "bbox_include_in_render": False
        },
        0x6A: {
            "name": "[Vector extension] Radiated object",
            "bbox": False,
            "bbox_include_in_render": False
        },
        0x6B: {
            "name": "[Vector extension] Skeleton for replications",
            "bbox": False,
            "bbox_include_in_render": False
        },
    }

    def __init__(self, filename: str):
        self.filename: str = filename

        # Read Drawfile into an array of bytes
        with open(filename, "rb") as file:
            self.bytes = file.read()

        # Read header of Drawfile
        self.size: int = len(self.bytes)
        self.draw_id: str = decode_riscos_string(self.bytes[0:4])
        self.major_version: int = bytes_to_uint(size=4, byte_array=self.bytes, position=4)
        self.minor_version: int = bytes_to_uint(size=4, byte_array=self.bytes, position=8)
        self.generator: str = decode_riscos_string(self.bytes[12:24])

        # Read bounding box from the file header
        self.x_min_as_read: int = bytes_to_int(size=4, byte_array=self.bytes, position=24)
        self.y_min_as_read: int = bytes_to_int(size=4, byte_array=self.bytes, position=28)
        self.x_max_as_read: int = bytes_to_int(size=4, byte_array=self.bytes, position=32)
        self.y_max_as_read: int = bytes_to_int(size=4, byte_array=self.bytes, position=36)

        # Initialise the working bounding box from object extents only (not the
        # header bbox) so that we can detect an insane header bbox afterwards.
        # Use sentinel values that will be replaced by the first factor_into_bbox call.
        self.x_min: int = 0x7FFFFFFF
        self.x_max: int = -0x80000000
        self.y_min: int = 0x7FFFFFFF
        self.y_max: int = -0x80000000

        # Read the objects that follow the header
        self.objects = []
        self.fetch_objects(target=self.objects, position=40, end_position=self.size)

        # Parse the font table to build a mapping of font index to font properties
        self.font_table: Dict[int, dict] = {}
        self._parse_font_table()

        # Decide which bounding box to use.  If the header bbox is sane, use it
        # (unioned with the object extents so nothing is clipped).  If the
        # header bbox is absurdly large, fall back to the computed object
        # extents only.
        max_size = 5  # metres — anything bigger than this is almost certainly wrong
        header_w = (self.x_max_as_read - self.x_min_as_read) * self.pixel
        header_h = (self.y_max_as_read - self.y_min_as_read) * self.pixel
        have_objects = self.x_min <= self.x_max and self.y_min <= self.y_max

        if header_w > max_size or header_h > max_size or header_w <= 0 or header_h <= 0:
            if have_objects:
                logging.info("Header bounding box is invalid (%.1f x %.1f m); "
                             "using bounding box computed from object extents",
                             header_w, header_h)
                # x_min/x_max/y_min/y_max are already set from object extents
            else:
                logging.warning("Header bounding box is invalid and no renderable "
                                "objects found; using header bbox as fallback")
                self.x_min = self.x_min_as_read
                self.x_max = self.x_max_as_read
                self.y_min = self.y_min_as_read
                self.y_max = self.y_max_as_read
        else:
            # Header bbox is sane — use it, expanded to include any objects
            # that fall outside it
            self.factor_into_bbox(x=self.x_min_as_read, y=self.y_min_as_read)
            self.factor_into_bbox(x=self.x_max_as_read, y=self.y_max_as_read)

    def factor_into_bbox(self, x: float, y: float) -> None:
        """
        Factor a point into the bounding box for this Drawfile
        :param x:
            Position, Drawfile pixels
        :param y:
            Position, Drawfile pixels
        """

        self.x_min = min(self.x_min, int(x - self.margin / self.pixel))
        self.x_max = max(self.x_max, int(x + self.margin / self.pixel))
        self.y_min = min(self.y_min, int(y - self.margin / self.pixel))
        self.y_max = max(self.y_max, int(y + self.margin / self.pixel))

    # Fallback mapping of RISC OS font family names to generic system fonts,
    # used when the bundled RISC OS fonts are not available.
    _riscos_font_fallbacks: Dict[str, str] = {
        "homerton": "FreeSans",
        "trinity": "FreeSerif",
        "corpus": "FreeMono",
        "newhall": "FreeSerif",
        "sassoon": "FreeSerif",
        "system": "FreeMono",
    }

    @staticmethod
    def _map_riscos_font(riscos_name: str) -> dict:
        """
        Map a RISC OS font name to system font properties.

        RISC OS font names follow the pattern: Family.Weight.Style
        e.g. "Trinity.Bold", "Homerton.Medium.Italic", "Trinity.Medium"

        When the bundled RISC OS fonts are registered with fontconfig, the
        original family name (e.g. "Trinity") is used directly. Otherwise
        falls back to FreeSans/FreeSerif/FreeMono.

        :param riscos_name:
            RISC OS font name string
        :return:
            Dictionary with keys: family, bold, italic
        """
        parts = riscos_name.split(".")
        family_key = parts[0].lower() if parts else ""

        if _RISCOS_FONTS_AVAILABLE:
            # Use the original RISC OS family name — fontconfig can resolve it
            # from the bundled OTF fonts
            family = parts[0] if parts else "Trinity"
        else:
            family = DrawFileRender._riscos_font_fallbacks.get(family_key, "FreeSerif")

        name_lower = riscos_name.lower()
        bold = "bold" in name_lower
        italic = "italic" in name_lower or "oblique" in name_lower

        return {"family": family, "bold": bold, "italic": italic}

    def _parse_font_table(self) -> None:
        """
        Find the font table object and parse it into a mapping of font index to font properties.
        """
        for obj in self.objects:
            if obj.get("type_name") == "Font table":
                payload_start = obj["position"] + 8  # no bbox, so payload is right after type+size
                payload_end = obj["position"] + obj["size"]
                pos = payload_start
                while pos < payload_end:
                    font_id = self.bytes[pos]
                    if font_id == 0:
                        break
                    pos += 1
                    null_pos = self.bytes.index(b"\x00", pos)
                    font_name = decode_riscos_string(self.bytes[pos:null_pos])
                    pos = null_pos + 1
                    self.font_table[font_id] = self._map_riscos_font(font_name)
                    logging.debug("Font {:d}: {:s} -> {}".format(font_id, font_name, self.font_table[font_id]))
                break

    def x_pos(self, x: float) -> float:
        """
        Convert Drawfile coordinates into page coordinates (metres)
        :param x:
            Position, Drawfile pixels
        :return:
            Position, metres
        """
        return (x - self.x_min) * self.pixel

    def y_pos(self, y: float) -> float:
        """
        Convert Drawfile coordinates into page coordinates (metres)
        :param y:
            Position, Drawfile pixels
        :return:
            Position, metres
        """
        return (self.y_max - y) * self.pixel

    def fetch_objects(self, target: list, position: int, end_position: int, exit_on_zero: bool = False) -> int:
        """
        Read the list of objects contained within a Drawfile, or within a parent object.

        :param target:
            The list into which we insert the objects we extract.
        :param position:
            The position of the start of the list of objects within the input file
        :param end_position:
            The maximum position in the file beyond which we should not read
        :param exit_on_zero:
            Exit if an object begins with a null word
        :return:
            Byte position after the end of the last object read
        """
        while position < end_position:
            new_object = self.fetch_object(position=position, exit_on_zero=exit_on_zero)
            if new_object is None:
                break
            target.append(new_object)
            position += new_object["size"]

            # Impose a minimum size on an object, as otherwise infinite recursion is possible
            if new_object["size"] < 8:
                logging.info("Drawfile object with illegal size of {:d} bytes".format(new_object["size"]))
                break

        # Return byte position after the end of the last object read
        return position

    def fetch_object(self, position: int, exit_on_zero: bool = False) -> Optional[Dict]:
        """
        Fetch a single object from the Drawfile.

        :param position:
            The position of the start of this object
        :param exit_on_zero:
            Exit if an object begins with a null word
        :return:
            A dictionary describing the object we extracted
        """
        # Read the object header
        type_id_32: int = bytes_to_uint(size=4, byte_array=self.bytes, position=position)

        # Draw Plus stores other flags in most significant 24 bits, so ignore these in determining object type
        type_id: int = type_id_32 & 0xFF

        # A zero indicates the end of the string of objects
        if type_id == 0 and exit_on_zero:
            return None

        # Create dictionary describing the object we are reading
        size: int = bytes_to_uint(size=4, byte_array=self.bytes, position=position + 4)
        new_object = {
            "type_id": type_id_32,
            "position": position,
            "size": size,
            "metadata": {}
        }

        # If this object is of an unknown type, we ignore it
        if type_id not in self.object_types:
            new_object["type_name"] = "Undefined type {:d}".format(type_id)
            return new_object

        # Populate the name of the type of this object
        type_info: dict = self.object_types[type_id]
        new_object["type_name"] = type_info["name"]

        # Populate the bounding box of this object
        if type_info["bbox"]:
            new_object["x_min"] = bytes_to_int(size=4, byte_array=self.bytes, position=position + 8)
            new_object["y_min"] = bytes_to_int(size=4, byte_array=self.bytes, position=position + 12)
            new_object["x_max"] = bytes_to_int(size=4, byte_array=self.bytes, position=position + 16)
            new_object["y_max"] = bytes_to_int(size=4, byte_array=self.bytes, position=position + 20)

            if type_info["bbox_include_in_render"]:
                # Only factor in the object bbox if it looks sane (not INT_MIN/INT_MAX
                # sentinel values that some applications write for unknown extents)
                obj_w = (new_object["x_max"] - new_object["x_min"]) * self.pixel
                obj_h = (new_object["y_max"] - new_object["y_min"]) * self.pixel
                if 0 < obj_w < 5 and 0 < obj_h < 5:
                    self.factor_into_bbox(x=new_object["x_min"], y=new_object["y_min"])
                    self.factor_into_bbox(x=new_object["x_max"], y=new_object["y_max"])

            payload_start: int = position + 24
        else:
            payload_start: int = position + 8

        # Read any children this object may have
        children_end: int = 0
        if "children_start" in type_info:
            new_object["children"] = []
            children_end = self.fetch_objects(target=new_object["children"],
                                              position=payload_start + type_info["children_start"],
                                              end_position=position + size,
                                              exit_on_zero=True)

        # Read fields
        if "fields" in type_info:
            # Calculate byte position of the start of the fields
            if "fields_after_children" in type_info:
                fields_start = children_end
            else:
                fields_start = payload_start

            # Fetch each field in turn
            for field_name, field_props in type_info["fields"].items():
                value = None
                if field_props[1] == "uint":
                    # Unsigned integer
                    value = bytes_to_uint(byte_array=self.bytes, size=field_props[2],
                                          position=fields_start + field_props[0])
                if field_props[1] == "int/65536":
                    # Fixed-point number &XXXX.XXXX
                    value = bytes_to_int(byte_array=self.bytes, size=field_props[2],
                                         position=fields_start + field_props[0]) / 65536.
                elif field_props[1] == "str":
                    if field_props[2] > 0:
                        # String of pre-defined length
                        start = fields_start + field_props[0]
                        value = decode_riscos_string(self.bytes[start:start + field_props[2]])
                    else:
                        # Null-terminated string
                        start = fields_start + field_props[0]
                        value = decode_riscos_string(self.bytes[start:].split(b"\x00")[0])
                    # Remove padding
                    value = value.strip()
                # Set metadata item value
                new_object["metadata"][field_name] = value

        # Read path components
        if type_id == 2:
            path_style: int = new_object["metadata"]["path_style"]
            # Bits 0-1: join style (0=miter, 1=round, 2=bevel)
            new_object["metadata"]["join_style"] = path_style & 0x03
            # Bits 2-3: end cap style (0=butt, 1=round, 2=square, 3=triangle)
            new_object["metadata"]["end_cap"] = (path_style >> 2) & 0x03
            # Bits 4-5: start cap style (0=butt, 1=round, 2=square, 3=triangle)
            new_object["metadata"]["start_cap"] = (path_style >> 4) & 0x03
            # Bit 6: winding rule (0=non-zero, 1=even-odd)
            new_object["metadata"]["winding_rule"] = (path_style >> 6) & 0x01
            # Bit 7: dash pattern present
            has_dash_pattern: bool = bool(path_style & 0x80)
            new_object["metadata"]["has_dash_pattern"] = has_dash_pattern
            # Bits 8-15: reserved
            # Bits 16-23: triangle cap width (1/16ths of line width, full width at base)
            new_object["metadata"]["triangle_cap_width"] = (path_style >> 16) & 0xFF
            # Bits 24-31: triangle cap length (1/16ths of line width)
            new_object["metadata"]["triangle_cap_length"] = (path_style >> 24) & 0xFF

            if has_dash_pattern:
                new_object["dash_pattern"] = self.fetch_dash_pattern(position=position + 40)
                payload_start: int = position + 40 + new_object["dash_pattern"]["size"]
            else:
                payload_start: int = position + 40

            new_object["path"] = self.fetch_path(position=payload_start)

        # Return this object
        return new_object

    def fetch_dash_pattern(self, position: int) -> dict:
        """
        Fetch a dash pattern from within a path object.

        :param position:
            The byte position of the start of the dash pattern
        :return:
            Dictionary of properties
        """

        # Create dictionary describing the dash pattern we are reading
        start: int = bytes_to_uint(size=4, byte_array=self.bytes, position=position + 0)
        item_count: int = bytes_to_uint(size=4, byte_array=self.bytes, position=position + 4)
        new_object = {
            "start": start,
            "item_count": item_count,
            "sequence": []
        }

        # Calculate the size of this dash pattern
        size: int = 8 + 4 * item_count
        new_object["size"] = size

        # Read dash pattern
        new_object["sequence"] = [bytes_to_uint(size=4, byte_array=self.bytes, position=position + 8 + 4 * index)
                                  for index in range(item_count)]

        # Return this dash pattern descriptor
        return new_object

    def fetch_path(self, position: int) -> List[Dict]:
        """
        Fetch a path from within a path object.

        :param position:
            The byte position of the start of the path
        :return:
            List of dictionaries of properties
        """

        new_path: List[Dict] = []

        terminate: bool = False
        while not terminate:
            element_type: int = bytes_to_uint(size=4, byte_array=self.bytes, position=position)

            length: Optional[int] = None
            new_component: Optional[dict] = None
            if element_type == 0:
                new_component = {'type': 'END'}
                length = 4
                terminate = True
            elif element_type == 2:
                new_component = {'type': 'MOVE',
                                 'x': bytes_to_int(size=4, byte_array=self.bytes, position=position + 4),
                                 'y': bytes_to_int(size=4, byte_array=self.bytes, position=position + 8)
                                 }
                self.factor_into_bbox(x=new_component['x'], y=new_component['y'])
                length = 12
            elif element_type == 5:
                new_component = {'type': 'CLOSE'}
                length = 4
            elif element_type == 6:
                new_component = {'type': 'BEZIER',
                                 'x0': bytes_to_int(size=4, byte_array=self.bytes, position=position + 4),
                                 'y0': bytes_to_int(size=4, byte_array=self.bytes, position=position + 8),
                                 'x1': bytes_to_int(size=4, byte_array=self.bytes, position=position + 12),
                                 'y1': bytes_to_int(size=4, byte_array=self.bytes, position=position + 16),
                                 'x2': bytes_to_int(size=4, byte_array=self.bytes, position=position + 20),
                                 'y2': bytes_to_int(size=4, byte_array=self.bytes, position=position + 24),
                                 }
                self.factor_into_bbox(x=new_component['x0'], y=new_component['y0'])
                self.factor_into_bbox(x=new_component['x1'], y=new_component['y1'])
                self.factor_into_bbox(x=new_component['x2'], y=new_component['y2'])
                length = 28
            elif element_type == 8:
                new_component = {'type': 'LINE',
                                 'x': bytes_to_int(size=4, byte_array=self.bytes, position=position + 4),
                                 'y': bytes_to_int(size=4, byte_array=self.bytes, position=position + 8)
                                 }
                self.factor_into_bbox(x=new_component['x'], y=new_component['y'])
                length = 12

            # If we got an item we can't parse, then finish gracefully
            if length is None:
                new_component = {'type': 'ILLEGAL'}
                length = 4
                terminate = True

            # Add this path element to the chain
            new_path.append(new_component)
            # Advance to next path element
            position += length

        # Return this path
        return new_path

    def describe_path(self, item: list, indent: int = 0) -> str:
        """
        Return a string describing the internal structure of a single path within a Drawfile.

        :param item:
            The list of elements describing the path we are to describe.
        :param indent:
            The number of indentation levels to the left of the text.
        :return:
            str
        """
        output = ""
        tab = "    " * indent
        output += "{:s}* Path has {:d} elements: {}\n".format(tab, len(item), repr(item))

        # Return string describing this path
        return output

    def describe_object(self, item: dict, indent: int = 0) -> str:
        """
        Return a string describing the internal structure of a single object within a Drawfile.

        :param item:
            The dictionary describing the object we are to describe.
        :param indent:
            The number of indentation levels to the left of the text.
        :return:
            str
        """
        output: str = ""
        tab: str = "    " * indent
        output += "{:s}* Object <{:s}>\n".format(tab, item["type_name"])
        output += "{:s}    * Type id       : {:08X}\n".format(tab, item["type_id"])
        output += "{:s}    * Byte position : {:d}\n".format(tab, item["position"])
        output += "{:s}    * Byte size     : {:d}\n".format(tab, item["size"])

        # Render object bounding box
        if "x_min" in item:
            output += "{:s}    * Bounding box X: {:8d} -> {:8d}\n".format(tab, item["x_min"], item["x_max"])
            output += "{:s}    * Bounding box Y: {:8d} -> {:8d}\n".format(tab, item["y_min"], item["y_max"])

        # Render object metadata
        for item_key in sorted(item["metadata"].keys()):
            item_value = item["metadata"][item_key]
            if "colour" in item_key.lower():
                item_value = "{:08X}".format(item_value)
            output += "{:s}    * {:14s}: {}\n".format(tab, item_key, str(item_value))

        # Render path, if present
        if "path" in item:
            output += self.describe_path(item=item["path"], indent=indent + 1)

        # Render object children
        if "children" in item:
            for item in item["children"]:
                output += self.describe_object(item=item, indent=indent + 1)

        # Return string describing this object
        return output

    def describe_contents(self) -> str:
        """
        Return a string describing the internal structure of this Drawfile

        :return:
            str
        """
        output = ""
        output += "File size     : {:d} bytes\n".format(self.size)
        output += "Draw ID       : {:s}\n".format(self.draw_id)
        output += "Major version : {:d}\n".format(self.major_version)
        output += "Minor version : {:d}\n".format(self.minor_version)
        output += "Generator     : {:s}\n".format(self.generator)
        output += "Bounding box X: {:8d} -> {:8d}\n".format(self.x_min_as_read, self.x_max_as_read)
        output += "Bounding box Y: {:8d} -> {:8d}\n".format(self.y_min_as_read, self.y_max_as_read)
        output += "Bounding box X: {:8d} -> {:8d} (computed)\n".format(self.x_min, self.x_max)
        output += "Bounding box Y: {:8d} -> {:8d} (computed)\n".format(self.y_min, self.y_max)

        for item in self.objects:
            output += self.describe_object(item=item)

        return output

    def render_object(self, item: dict, context: GraphicsContext) -> None:
        # Render text objects
        if item['type_name'] in ("Text object", "Transformed text object"):
            text_string: str = item["metadata"]["text"]
            text_colour: Sequence[float] = context_colour_from_int(item["metadata"]["text_colour"])
            context.set_color(color=text_colour)

            # Select font face from the font table
            font_id: int = item["metadata"]["text_style"]
            if font_id in self.font_table:
                font_props = self.font_table[font_id]
                context.set_font_style(family=font_props["family"],
                                       bold=font_props["bold"],
                                       italic=font_props["italic"])
            else:
                context.set_font_style(family="FreeSerif", bold=False, italic=False)

            # Use the font size from the Draw file (y_size in Draw units)
            font_size_metres: float = item["metadata"]["y_size"] * self.pixel
            font_size: float = font_size_metres / context.base_font_size

            # Position text at the baseline coordinates from the Draw file
            x_pos: float = self.x_pos(x=item["metadata"]["x_baseline"])
            y_pos: float = self.y_pos(y=item["metadata"]["y_baseline"])

            if item['type_name'] == "Transformed text object":
                # Apply transformation matrix
                xx: float = item["metadata"]["transformation_a"]
                yx: float = -item["metadata"]["transformation_b"]
                xy: float = -item["metadata"]["transformation_c"]
                yy: float = item["metadata"]["transformation_d"]
                context.matrix_transformation_set(xx=xx, yx=yx, xy=xy, yy=yy, x0=0, y0=0,
                                                  centre_x=x_pos, centre_y=y_pos
                                                  )

                context.set_font_size(font_size=font_size)
                context.text(text=text_string, h_align=-1, v_align=-1, gap=0, rotation=0, x=0, y=0)

                # Undo transformation
                context.matrix_transformation_restore()
            else:
                context.set_font_size(font_size=font_size)
                context.text(text=text_string, h_align=-1, v_align=-1, gap=0, rotation=0, x=x_pos, y=y_pos)

        # Render text area objects
        if item['type_name'] == "Text area object":
            text_string: str = item["metadata"]["text"]
            clean_text: str = re.sub(r"\s+", " ", re.sub(r"\\[\d]", "", re.sub(r"\\[^\d][^/\n]*/?", "", text_string)))
            text_colour: Sequence[float] = context_colour_from_int(item["metadata"]["colour_foreground"])
            x_centre: float = self.x_pos(x=(item["x_max"] + item["x_min"]) / 2)
            y_centre: float = self.y_pos(y=(item["y_max"] + item["y_min"]) / 2)
            target_width: float = (item["x_max"] - item["x_min"]) * self.pixel

            context.set_font_size(font_size=1)
            context.set_color(color=text_colour)
            context.text_wrapped(text=clean_text, x=x_centre, y=y_centre, h_align=0, v_align=0, width=target_width,
                                 justify=0)
        # Render path objects
        if item['type_name'] == "Path object":
            meta = item['metadata']
            start_cap: int = meta['start_cap']
            end_cap: int = meta['end_cap']

            # Collect open-subpath endpoint data now if triangle caps are needed,
            # before the Cairo rendering loop consumes the path list.
            needs_triangle_caps: bool = (start_cap == 3 or end_cap == 3)
            open_subpaths: List[Dict] = (
                _collect_open_subpaths(item['path']) if needs_triangle_caps else []
            )

            # Set the winding/fill rule for this path (bit 6 of path_style)
            context.set_fill_rule(even_odd=bool(meta['winding_rule']))

            # Start path
            context.begin_path()

            # Trace path, point by point
            for path_item in item['path']:
                if path_item['type'] == 'END':
                    break
                elif path_item['type'] == 'MOVE':
                    context.move_to(x=self.x_pos(x=path_item['x']), y=self.y_pos(y=path_item['y']))
                elif path_item['type'] == 'CLOSE':
                    context.close_path()
                    context.begin_sub_path()
                elif path_item['type'] == 'BEZIER':
                    context.curve_to(x0=self.x_pos(x=path_item['x0']), y0=self.y_pos(y=path_item['y0']),
                                     x1=self.x_pos(x=path_item['x1']), y1=self.y_pos(y=path_item['y1']),
                                     x2=self.x_pos(x=path_item['x2']), y2=self.y_pos(y=path_item['y2']))
                elif path_item['type'] == 'LINE':
                    context.line_to(x=self.x_pos(x=path_item['x']), y=self.y_pos(y=path_item['y']))

            # Fill path
            fill_colour = context_colour_from_int(uint=item['metadata']['fill_colour'])
            if fill_colour[3] > 0:
                context.fill(color=fill_colour)

            # Stroke path
            stroke_colour = context_colour_from_int(uint=item['metadata']['outline_colour'])
            outline_width = max(1, meta['outline_width'] * self.pixel / context.base_line_width)
            if stroke_colour[3] > 0:
                # Convert dash pattern from Draw units to metres
                dash_pattern = None
                if meta['has_dash_pattern'] and 'dash_pattern' in item:
                    dash_pattern = [v * self.pixel for v in item['dash_pattern']['sequence']]

                # Cairo applies one cap style to both path ends. For triangle caps (style 3)
                # always use BUTT so the line ends flush at the triangle base; the arrowhead
                # is drawn as a separate filled polygon. When only one end is a triangle,
                # apply the other end's style to the stroke.
                if start_cap == 3 and end_cap == 3:
                    cairo_cap: int = 0
                elif start_cap == 3:
                    cairo_cap = end_cap
                elif end_cap == 3:
                    cairo_cap = start_cap
                else:
                    cairo_cap = end_cap

                context.stroke(color=stroke_colour, line_width=outline_width,
                               dotted=meta['has_dash_pattern'], dash_pattern=dash_pattern,
                               line_cap=cairo_cap, line_join=meta['join_style'])

                # Draw triangle caps as filled polygons after the stroke.
                # Direction vectors are in Draw units; y_pos() flips Y, so negate dy
                # when converting Draw-space direction to Cairo space.
                if needs_triangle_caps and open_subpaths:
                    line_width_m: float = meta['outline_width'] * self.pixel
                    half_cap_width_m: float = (meta['triangle_cap_width'] / 16.0) * line_width_m / 2.0
                    cap_length_m: float = (meta['triangle_cap_length'] / 16.0) * line_width_m

                    for sp in open_subpaths:
                        if start_cap == 3:
                            # Outward direction at start = reverse of path direction
                            _draw_triangle_cap(context,
                                               self.x_pos(sp['start_x']), self.y_pos(sp['start_y']),
                                               -sp['start_dx'], sp['start_dy'],
                                               cap_length_m, half_cap_width_m, stroke_colour)
                        if end_cap == 3:
                            # Outward direction at end = path direction (dy negated for Cairo)
                            _draw_triangle_cap(context,
                                               self.x_pos(sp['end_x']), self.y_pos(sp['end_y']),
                                               sp['end_dx'], -sp['end_dy'],
                                               cap_length_m, half_cap_width_m, stroke_colour)

        # Render sprite objects
        if item['type_name'] in ("Sprite object", "Transformed sprite object"):
            if item['type_name'] == "Sprite object":
                preface_size = 24
            else:
                preface_size = 48
            block_position = item["position"] + preface_size  # Start of sprite data
            block_size = item["size"] - preface_size  # Number of bytes of sprite data
            sprite_bytes = self.bytes[block_position:block_position + block_size]

            # Construct a sprite file containing this sprite
            sprite_file_handle = io.BytesIO()
            # Number of sprites in area
            sprite_file_handle.write((1).to_bytes(length=4, byteorder='little'))
            # Offset to first sprite
            sprite_file_handle.write((0x10).to_bytes(length=4, byteorder='little'))
            # Offset to first free word in area (i.e. after last sprite)
            free = bytes_to_uint(size=4, byte_array=sprite_bytes, position=0) + 0x10
            sprite_file_handle.write(free.to_bytes(length=4, byteorder='little'))
            # Sprite data
            sprite_file_handle.write(sprite_bytes)

            # Convert it into a sprite object
            try:
                sprite = spritefile.spritefile(file=sprite_file_handle)
                with temporary_directory.TemporaryDirectory() as tmp_dir:
                    spr2img.convert_sprites(spr=sprite, output_dir=tmp_dir.tmp_dir, format="png")
                    first_sprite = glob.glob(os.path.join(tmp_dir.tmp_dir, "*.png"))[0]

                    # Render sprite
                    if item['type_name'] == "Transformed sprite object":
                        centre_x: float = self.x_pos((item["x_max"] + item["x_min"]) / 2)
                        centre_y: float = self.y_pos((item["y_max"] + item["y_min"]) / 2)
                        target_width: float = (item["x_max"] - item["x_min"]) * self.pixel
                        target_height: float = (item["y_max"] - item["y_min"]) * self.pixel

                        # Apply transformation to sprite
                        xx: float = item["metadata"]["transformation_a"]
                        yx: float = -item["metadata"]["transformation_b"]
                        xy: float = -item["metadata"]["transformation_c"]
                        yy: float = item["metadata"]["transformation_d"]
                        context.matrix_transformation_set(xx=xx, yx=yx, xy=xy, yy=yy, x0=0, y0=0,
                                                          centre_x=centre_x, centre_y=centre_y
                                                          )

                        # Work out the correct scaling to fill the bounding box
                        corners = [(0.5 * sgn_x, 0.5 * sgn_y) for sgn_x in (-1, 1) for sgn_y in (-1, 1)]
                        corners_transformed = [(p[0] * xx + p[1] * xy, p[0] * yx + p[1] * yy) for p in corners]
                        transformed_unit_width = (max([p[0] for p in corners_transformed]) -
                                                  min([p[0] for p in corners_transformed]))
                        transformed_unit_height = (max([p[1] for p in corners_transformed]) -
                                                   min([p[1] for p in corners_transformed]))
                        target_width_transformed = target_width / transformed_unit_width
                        target_height_transformed = target_height / transformed_unit_height

                        # Paint sprite onto the canvas
                        context.paint_png_image(png_filename=first_sprite,
                                                x_left=-target_width_transformed / 2,
                                                y_top=-target_height_transformed / 2,
                                                target_width=target_width_transformed,
                                                target_height=target_height_transformed
                                                )

                        # Undo transformation
                        context.matrix_transformation_restore()
                    else:
                        # Paint sprite onto the canvas
                        context.paint_png_image(png_filename=first_sprite,
                                                x_left=self.x_pos(x=item["x_min"]),
                                                y_top=self.y_pos(y=item["y_max"]),
                                                target_width=(item["x_max"] - item["x_min"]) * self.pixel,
                                                target_height=(item["y_max"] - item["y_min"]) * self.pixel
                                                )

            except Exception:
                logging.info("Failed to render sprite", exc_info=True)

        # Render object children
        if "children" in item:
            for item in item["children"]:
                self.render_object(item=item, context=context)

    def render_to_context(self, filename: str, img_format: str, dots_per_inch: float = 72.) -> None:
        """
        Render this Draw file to a graphics page.
        """

        with GraphicsPage(img_format=img_format, output=filename, dots_per_inch=dots_per_inch,
                          width=(self.x_max - self.x_min) * self.pixel,
                          height=(self.y_max - self.y_min) * self.pixel
                          ) as page:
            with GraphicsContext(page=page, offset_x=0, offset_y=0) as context:
                for item in self.objects:
                    self.render_object(item=item, context=context)


# Do it right away if we're run as a script
if __name__ == "__main__":
    # Read input parameters
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',
                        default="my_drawfile.aff",
                        type=str,
                        dest="input_filename",
                        help="Input Draw file to process")
    parser.add_argument('--output',
                        default="/tmp/my_drawfile",
                        type=str,
                        dest="output_filename",
                        help="Output destination for PNG output")
    parser.add_argument('--debug',
                        action='store_true',
                        dest="debug",
                        help="Show full debugging output")
    parser.set_defaults(debug=False)
    args = parser.parse_args()

    # Set up a logging object
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        stream=sys.stdout,
                        format='[%(asctime)s] %(levelname)s:%(filename)s:%(message)s',
                        datefmt='%d/%m/%Y %H:%M:%S')
    logger = logging.getLogger(__name__)
    logger.debug(__doc__.strip())

    # Open input file
    df = DrawFileRender(filename=args.input_filename)
    print(df.describe_contents())
    df.render_to_context(filename=args.output_filename, img_format="png")
