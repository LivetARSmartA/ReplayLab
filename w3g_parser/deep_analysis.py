from __future__ import annotations
from dataclasses import dataclass, field
DEEP_ANALYSIS_SCHEMA_VERSION = 3
EARNED_GOLD_LEDGER_CONTRACT_ID = 'earned-gold-ledger-v2'
EVENT_EXACT_CONFIDENCE = 'event-exact-runtime'
SAMPLED_EXACT_CONFIDENCE = 'sampled-exact-runtime'
WALLET_LEDGER_SOURCE = 'game-dll-set-player-state'
HERO_XP_SOURCE = 'game-dll-hero-xp'
EARNED_GOLD_SOURCE = 'validated-earned-gold-ledger'
NET_WORTH_SOURCE = 'runtime-owned-assets'

@dataclass(frozen=True)
class DeepAnalysisIdentity:
    replay_sha256: str
    game_dll_sha256: str
    analysis_profile_id: str
    ledger_contract_id: str = EARNED_GOLD_LEDGER_CONTRACT_ID
    cache_key_digest: str = ''

    @property
    def is_valid(self) -> bool:
        return bool(self.cache_key_digest)

    @property
    def cache_key(self) -> str:
        if not self.cache_key_digest:
            raise ValueError('Deep Analysis identity was not validated by C++')
        return self.cache_key_digest

@dataclass(frozen=True)
class WalletLedgerPoint:
    player_slot: int
    game_time_ms: int
    wallet: int
    cumulative_inflow: int
    cumulative_outflow: int
    delta: int
    classification: str
    source: str = WALLET_LEDGER_SOURCE
    confidence: str = EVENT_EXACT_CONFIDENCE

    @property
    def gross_gpm(self) -> float | None:
        if self.game_time_ms <= 0:
            return None
        return self.cumulative_inflow * 60000 / self.game_time_ms

@dataclass(frozen=True)
class WalletLedger:
    player_slot: int
    game_start_wallet: int | None
    final_wallet: int | None
    points: tuple[WalletLedgerPoint, ...]
    probe_pairs: int
    issues: tuple[str, ...]
    source: str = WALLET_LEDGER_SOURCE
    confidence: str = EVENT_EXACT_CONFIDENCE

    @property
    def is_valid(self) -> bool:
        return self.game_start_wallet is not None and self.final_wallet is not None and bool(self.points) and (not self.issues)

@dataclass(frozen=True)
class HeroXpPoint:
    player_slot: int
    game_time_ms: int
    xp: int
    source: str = HERO_XP_SOURCE
    confidence: str = SAMPLED_EXACT_CONFIDENCE

    @property
    def cumulative_xpm(self) -> float | None:
        if self.game_time_ms <= 0:
            return None
        return self.xp * 60000 / self.game_time_ms

@dataclass(frozen=True)
class HeroXpTimeline:
    player_slot: int
    points: tuple[HeroXpPoint, ...]
    issues: tuple[str, ...]
    source: str = HERO_XP_SOURCE
    confidence: str = SAMPLED_EXACT_CONFIDENCE

    @property
    def is_valid(self) -> bool:
        return bool(self.points) and (not self.issues)

@dataclass(frozen=True)
class EarnedGoldPoint:
    player_slot: int
    game_time_ms: int
    earned_gold_tenths: int
    source: str = EARNED_GOLD_SOURCE
    confidence: str = EVENT_EXACT_CONFIDENCE

    @property
    def earned_gold(self) -> float:
        return self.earned_gold_tenths / 10.0

    @property
    def gpm(self) -> float | None:
        if self.game_time_ms <= 0:
            return None
        return self.earned_gold_tenths * 6000 / self.game_time_ms

@dataclass(frozen=True)
class EarnedGoldTimeline:
    player_slot: int
    points: tuple[EarnedGoldPoint, ...]
    issues: tuple[str, ...]
    source: str = EARNED_GOLD_SOURCE
    confidence: str = EVENT_EXACT_CONFIDENCE

    @property
    def is_valid(self) -> bool:
        return bool(self.points) and (not self.issues)

@dataclass(frozen=True)
class NetWorthPoint:
    player_slot: int
    game_time_ms: int
    wallet: int
    owned_item_value: int
    source: str = NET_WORTH_SOURCE
    confidence: str = SAMPLED_EXACT_CONFIDENCE

    @property
    def net_worth(self) -> int:
        return self.wallet + self.owned_item_value

@dataclass(frozen=True)
class NetWorthTimeline:
    player_slot: int
    points: tuple[NetWorthPoint, ...]
    issues: tuple[str, ...]
    containers: tuple[str, ...] = ()
    source: str = NET_WORTH_SOURCE
    confidence: str = SAMPLED_EXACT_CONFIDENCE

    @property
    def is_valid(self) -> bool:
        return bool(self.points) and bool(self.containers) and (not self.issues)

@dataclass(frozen=True)
class DeepAnalysisBundle:
    schema_version: int
    identity: DeepAnalysisIdentity
    duration_ms: int
    wallet_ledgers: dict[int, WalletLedger]
    hero_xp_timelines: dict[int, HeroXpTimeline]
    earned_gold_timelines: dict[int, EarnedGoldTimeline] = field(default_factory=dict)
    net_worth_timelines: dict[int, NetWorthTimeline] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        timelines = (*self.wallet_ledgers.values(), *self.hero_xp_timelines.values(), *self.earned_gold_timelines.values(), *self.net_worth_timelines.values())
        return self.schema_version == DEEP_ANALYSIS_SCHEMA_VERSION and self.identity.is_valid and (self.duration_ms > 0) and any((timeline.is_valid for timeline in timelines))

    def timeline_for(self, metric: str, player_slot: int) -> object | None:
        collections: dict[str, dict[int, object]] = {'gpm': self.earned_gold_timelines, 'xpm': self.hero_xp_timelines, 'net_worth': self.net_worth_timelines}
        return collections.get(metric, {}).get(player_slot)

    def valid_slots_for(self, metric: str) -> tuple[int, ...]:
        collection = {'gpm': self.earned_gold_timelines, 'xpm': self.hero_xp_timelines, 'net_worth': self.net_worth_timelines}.get(metric, {})
        return tuple((slot for slot, timeline in sorted(collection.items()) if timeline.is_valid))
