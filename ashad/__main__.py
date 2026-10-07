import argparse


def main():
    parser = argparse.ArgumentParser(description="ASHAD controller, owned by the ASHAD desktop app")
    parser.add_argument("--desktop", action="store_true", help="Owned Electron controller; bounded configuration arrives on stdin")
    args = parser.parse_args()
    if not args.desktop:
        raise SystemExit("ASHAD runs as a desktop app. Launch ASHAD.exe; its private controller starts automatically.")
    from .desktop import main as desktop_main
    try:
        desktop_main()
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
