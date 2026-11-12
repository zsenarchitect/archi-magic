"""Export or restore an office project's review state while the service is stopped."""

import argparse
import json
from pathlib import Path

from workspace_review_state import ReviewState


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "import"))
    parser.add_argument("project", help="Project slug from the workspace configuration")
    parser.add_argument("root", type=Path, help="Absolute physical root from the workspace configuration")
    parser.add_argument("snapshot", type=Path, help="Private snapshot file outside the project root")
    args = parser.parse_args()

    try:
        root = args.root.resolve(strict=True)
        if root != args.root or not root.is_dir():
            raise ValueError("Use the absolute physical project root.")
        snapshot = args.snapshot.parent.resolve(strict=True) / args.snapshot.name
        if snapshot.is_relative_to(root):
            raise ValueError("Keep the snapshot outside the project root.")
        with ReviewState(root, args.project, {}, ()) as store:
            if args.action == "export":
                revision = store.export_snapshot(snapshot)
            else:
                revision = store.import_snapshot(snapshot)
    except BlockingIOError:
        parser.exit(2, "The project lock is busy. Stop the workspace before maintenance.\n")
    except FileExistsError:
        parser.exit(2, "The destination already exists. Choose a new export path or an empty restore root.\n")
    except (OSError, ValueError, RecursionError) as error:
        parser.exit(2, f"Workspace maintenance stopped ({type(error).__name__}). Check project and snapshot.\n")

    print(json.dumps({"action": args.action, "project": args.project, "etag": revision}))


if __name__ == "__main__":
    main()
