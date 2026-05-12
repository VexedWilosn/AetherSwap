from __future__ import annotations

import argparse

from DataEngine.steamdt_fetcher import register_steamdt_capsule_from_cookie


def main() -> None:
    parser = argparse.ArgumentParser(description="Register a SteamDT session capsule from a cookie string.")
    parser.add_argument("--cookie", required=True, help="Raw SteamDT cookie string")
    parser.add_argument("--user-agent", default="", help="Optional browser user-agent")
    parser.add_argument("--device-id", default="", help="Optional explicit SDT_DeviceId")
    parser.add_argument("--proxy-binding", default="direct", choices=["direct", "pool"], help="Bind capsule to direct or proxy usage")
    parser.add_argument("--notes", default="manual import", help="Optional capsule note")
    args = parser.parse_args()

    capsule = register_steamdt_capsule_from_cookie(
        args.cookie,
        user_agent=args.user_agent,
        device_id=args.device_id,
        proxy_binding=args.proxy_binding,
        notes=args.notes,
    )
    print(
        {
            "capsule_id": capsule.capsule_id,
            "platform": capsule.platform,
            "device_id": capsule.device_id,
            "proxy_binding": capsule.proxy_binding,
            "status": capsule.status,
        }
    )


if __name__ == "__main__":
    main()
