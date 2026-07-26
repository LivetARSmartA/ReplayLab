from __future__ import annotations
import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

class WarcraftBuildError(ValueError):
    pass

@dataclass(frozen=True)
class PeSectionLayout:
    name: str
    virtual_address: int
    virtual_size: int
    raw_offset: int
    raw_size: int
    characteristics: int

@dataclass(frozen=True)
class PeImageLayout:
    machine: int
    timestamp: int
    optional_magic: int
    image_base: int
    entry_point_rva: int
    size_of_image: int
    characteristics: int
    sections: tuple[PeSectionLayout, ...]

@dataclass(frozen=True)
class BuildAnchor:
    name: str
    rva: int
    size: int
    sha256: str

@dataclass(frozen=True)
class GameDllProfile:
    key: str
    label: str
    exact_sha256: frozenset[str]
    layout: PeImageLayout
    anchors: tuple[BuildAnchor, ...]

@dataclass(frozen=True)
class GameDllMatch:
    path: Path
    sha256: str
    profile_key: str
    profile_label: str
    match_kind: str

    @property
    def exact(self) -> bool:
        return self.match_kind == 'exact'
LEGACY_126A_LAYOUT = PeImageLayout(machine=332, timestamp=1300478720, optional_magic=267, image_base=1862270976, entry_point_rva=8263096, size_of_image=12275712, characteristics=8450, sections=(PeSectionLayout('.text', 4096, 8828426, 4096, 8830976, 1610612768), PeSectionLayout('.rdata', 8835072, 1970628, 8835072, 1974272, 1073741888), PeSectionLayout('.data', 10809344, 624044, 10809344, 393216, 3221225536), PeSectionLayout('.rsrc', 11436032, 1388, 11202560, 4096, 1073741888), PeSectionLayout('.reloc', 11440128, 831710, 11206656, 835584, 1107296320)))
LEGACY_126A_PROFILE = GameDllProfile(key='legacy-1.26a-6401', label='Warcraft III 1.26a (6401)', exact_sha256=frozenset({'6D21BD9A0F9FBC8446F455C9E89AC994FED68174426FE608FCB9BAEFD4DEC53C'}), layout=LEGACY_126A_LAYOUT, anchors=(BuildAnchor('rdata-head', 8835072, 256, '4E0CB97C67A9FBDAF70D4099233D74563C3D29CDA5E7EE6BC799C7404DCC9EF0'), BuildAnchor('unit-vtable-window', 9640180, 256, '44BDCA2A7938E189BDAEADF367EA8C9CEE93F9DA67AB79E564AA9136A7F6E478'), BuildAnchor('rdata-tail', 10809088, 256, '5341E6B2646979A70E57653007A1F310169421EC9BDD9F1A5648F75ADE005AF1'), BuildAnchor('data-head', 10809344, 256, '44926C2D26E101C1044C229D671DB765418ED0FB844445BED154D1C000E2411D')))

def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()

def _parse_pe_layout(payload: bytes) -> PeImageLayout:
    if len(payload) < 64 or payload[:2] != b'MZ':
        raise WarcraftBuildError('Game.dll is not a valid PE image')
    pe_offset = struct.unpack_from('<I', payload, 60)[0]
    if pe_offset < 64 or pe_offset + 24 > len(payload):
        raise WarcraftBuildError('Game.dll has an invalid PE header offset')
    if payload[pe_offset:pe_offset + 4] != b'PE\x00\x00':
        raise WarcraftBuildError('Game.dll has no PE signature')
    machine, section_count, timestamp, _symbol_table, _symbol_count, optional_size, characteristics = struct.unpack_from('<HHIIIHH', payload, pe_offset + 4)
    optional_offset = pe_offset + 24
    if optional_size < 60 or optional_offset + optional_size > len(payload):
        raise WarcraftBuildError('Game.dll has a truncated optional header')
    optional_magic = struct.unpack_from('<H', payload, optional_offset)[0]
    if optional_magic != 267:
        raise WarcraftBuildError('Game.dll must be a 32-bit PE image')
    entry_point_rva = struct.unpack_from('<I', payload, optional_offset + 16)[0]
    image_base = struct.unpack_from('<I', payload, optional_offset + 28)[0]
    size_of_image = struct.unpack_from('<I', payload, optional_offset + 56)[0]
    if section_count <= 0 or section_count > 32:
        raise WarcraftBuildError('Game.dll has an invalid PE section count')
    section_offset = optional_offset + optional_size
    if section_offset + section_count * 40 > len(payload):
        raise WarcraftBuildError('Game.dll has a truncated PE section table')
    sections: list[PeSectionLayout] = []
    for index in range(section_count):
        offset = section_offset + index * 40
        name = payload[offset:offset + 8].split(b'\x00', 1)[0].decode('ascii', errors='replace')
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from('<IIII', payload, offset + 8)
        section_characteristics = struct.unpack_from('<I', payload, offset + 36)[0]
        if raw_size and raw_offset + raw_size > len(payload):
            raise WarcraftBuildError(f'Game.dll section {name or index} exceeds the file size')
        sections.append(PeSectionLayout(name=name, virtual_address=virtual_address, virtual_size=virtual_size, raw_offset=raw_offset, raw_size=raw_size, characteristics=section_characteristics))
    return PeImageLayout(machine=machine, timestamp=timestamp, optional_magic=optional_magic, image_base=image_base, entry_point_rva=entry_point_rva, size_of_image=size_of_image, characteristics=characteristics, sections=tuple(sections))

def inspect_pe_layout(path: str | Path) -> PeImageLayout:
    return _parse_pe_layout(Path(path).read_bytes())

def _rva_payload(payload: bytes, layout: PeImageLayout, rva: int, size: int) -> bytes:
    if size <= 0:
        raise WarcraftBuildError('Build anchor size must be positive')
    for section in layout.sections:
        section_end = section.virtual_address + section.raw_size
        if section.virtual_address <= rva and rva + size <= section_end:
            offset = section.raw_offset + rva - section.virtual_address
            return payload[offset:offset + size]
    raise WarcraftBuildError(f'Build anchor RVA 0x{rva:08X} is not in the file')

def match_game_dll(path: str | Path, profile: GameDllProfile=LEGACY_126A_PROFILE) -> GameDllMatch:
    dll_path = Path(path)
    payload = dll_path.read_bytes()
    digest = _sha256(payload)
    actual_layout = _parse_pe_layout(payload)
    if actual_layout != profile.layout:
        raise WarcraftBuildError(f'Game.dll layout does not match {profile.label}; SHA-256 {digest}')
    failed_anchors: list[str] = []
    for anchor in profile.anchors:
        anchor_payload = _rva_payload(payload, actual_layout, anchor.rva, anchor.size)
        if _sha256(anchor_payload) != anchor.sha256:
            failed_anchors.append(anchor.name)
    if failed_anchors:
        names = ', '.join(failed_anchors)
        raise WarcraftBuildError(f'Game.dll failed critical {profile.label} anchors: {names}; SHA-256 {digest}')
    return GameDllMatch(path=dll_path, sha256=digest, profile_key=profile.key, profile_label=profile.label, match_kind='exact' if digest in profile.exact_sha256 else 'layout-compatible')
