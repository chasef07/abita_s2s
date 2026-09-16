"""Run checked-in scenarios through a local agent and LiveKit's audio simulator."""

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenarios', type=Path, default=ROOT / 'evals/terminal-transfer-baseline.yaml')
    parser.add_argument('--concurrency', type=int, default=2)
    parser.add_argument('--export', metavar='RUN_ID')
    parser.add_argument('--mock-transfer', action='store_true', help='End native audio simulations at transfer; never dial a phone number')
    args = parser.parse_args()
    os.chdir(ROOT)
    load_dotenv(ROOT / '.env.local', override=False)
    required = ['LIVEKIT_URL', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET']
    if not args.export:
        required += ['OPENAI_API_KEY', 'SANDBOX_AMD_API_URL', 'SANDBOX_AMD_API_TOKEN']
    missing = [key for key in required if not os.getenv(key, '').strip()]
    if missing:
        parser.error('Missing configuration: ' + ', '.join(missing))
    os.environ['PATH'] = str(ROOT / '.venv/bin') + os.pathsep + os.environ['PATH']
    if args.export:
        command = ['lk', 'agent', 'simulate', '--export', args.export]
    else:
        if not args.scenarios.is_file():
            parser.error('Scenario file does not exist')
        if not 1 <= args.concurrency <= 4:
            parser.error('Use concurrency between 1 and 4')
        entrypoint = 'scripts/voice_baseline_agent.py' if args.mock_transfer else 'src/abita_s2s/main.py'
        command = ['lk', 'agent', 'simulate', '--scenarios', str(args.scenarios),
                   '--concurrency', str(args.concurrency), 'audio', entrypoint]
    os.execvp('lk', command)
