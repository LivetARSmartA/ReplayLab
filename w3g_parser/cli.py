from __future__ import annotations
import argparse
import sys
from pathlib import Path
from .parser import ReplayParseError, parse_replay

def format_time(milliseconds: int) -> str:
    sign = '−' if milliseconds < 0 else ''
    milliseconds = abs(milliseconds)
    total_seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        value = f'{hours:d}:{minutes:02d}:{seconds:02d}.{millis:03d}'
    else:
        value = f'{minutes:d}:{seconds:02d}.{millis:03d}'
    return sign + value

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Parse a Warcraft III .w3g replay without launching the game.')
    parser.add_argument('replay', type=Path, help='Path to a .w3g replay')
    parser.add_argument('--json', type=Path, help='Write the complete report as JSON')
    parser.add_argument('--include-packets', action='store_true', help='Include every raw command-packet summary in JSON')
    parser.add_argument('--show-strings', action='store_true', help='Print candidate strings found inside player command streams')
    parser.add_argument('--show-events', action='store_true', help='Print DotA skill learns, kill events and derived multi-kills')
    parser.add_argument('--show-stats', action='store_true', help='Print the final DotA player statistics and APM')
    return parser

def main() -> int:
    arguments = build_parser().parse_args()
    try:
        report = parse_replay(arguments.replay)
    except (OSError, ReplayParseError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    print(f"Game: {report.game_name or '(unknown)'}")
    print(f"Map: {report.map_path or '(unknown)'}")
    print(f'Warcraft: {report.header.product} {report.header.version}, build {report.header.build}')
    print(f'Duration: {format_time(report.header.duration_ms)} (parsed timeline: {format_time(report.parsed_timeline_ms)})')
    print('Players: ' + ', '.join((f"{player.player_id}:{player.name or '(empty)'}" for player in report.players)))
    print(f'Commands: {len(report.command_packets)}, chats: {len(report.chats)}, gamecache syncs: {len(report.gamecache_syncs)}, candidate strings: {len(report.string_candidates)}')
    print(f'DotA kills: {len(report.kills)}, skill learns: {len(report.skill_learns)}, triple/ultra/rampage events: {sum((event.count >= 3 for event in report.multi_kills))}')
    if report.dota_players:
        print('Heroes: ' + ', '.join((f"{player.name}={player.hero_name or player.hero_rawcode or '?'}" for player in report.dota_players)))
    if report.leaves:
        print('Leaves: ' + ', '.join((f'P{event.player_id}@{format_time(event.time_ms)}' for event in report.leaves)))
    if arguments.show_strings:
        for item in report.string_candidates:
            print(f'[{format_time(item.time_ms)}] P{item.player_id} {item.source}: {item.text}')
    if arguments.show_events:
        print('DotA skill-build timeline:')
        for event in report.skill_learns:
            player = event.player_name
            if event.hero_name:
                player += f' ({event.hero_name})'
            ability = event.ability_name or event.ability_rawcode
            print(f'[{format_time(event.game_time_ms)}] {player}: {ability} level {event.new_level} [{event.ability_rawcode}; {event.confidence}]')
        print('DotA kill timeline:')
        for event in report.kills:
            killer = event.killer_name
            if event.killer_hero_name:
                killer += f' ({event.killer_hero_name})'
            victim = event.victim_name
            if event.victim_hero_name:
                victim += f' ({event.victim_hero_name})'
            print(f'[{format_time(event.game_time_ms)}] {killer} -> {victim}')
        print('Multi-kills:')
        for event in report.multi_kills:
            if event.count < 3:
                continue
            killer = event.killer_name
            if event.killer_hero_name:
                killer += f' ({event.killer_hero_name})'
            print(f"[{format_time(event.game_time_ms)}] {event.label}: {killer} -> {', '.join(event.victim_names)}")
    if arguments.show_stats:
        print('Final DotA statistics:')
        for player in report.dota_players:
            result = 'win' if player.won is True else 'loss' if player.won is False else 'unknown'
            kda = f"{(player.kills if player.kills is not None else '-')}/{(player.deaths if player.deaths is not None else '-')}/{(player.assists if player.assists is not None else '-')}"
            average_apm = '-' if player.apm_average is None else f'{player.apm_average:.1f}'
            print(f"{player.name} ({player.hero_name or player.hero_rawcode or '?'}): {result}, K/D/A {kda}, creeps {player.creep_kills}, denies {player.creep_denies}, neutral {player.neutral_kills}, gold {player.final_gold}, net worth {player.net_worth}, APM avg/peak {average_apm}/{player.apm_peak_60s}")
    if report.warnings:
        print('Warnings:')
        for warning in report.warnings:
            print(f'  - {warning}')
    if arguments.json:
        report.write_json(arguments.json, include_packets=arguments.include_packets)
        print(f'JSON: {arguments.json.resolve()}')
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
