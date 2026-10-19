from ingest.kb_common import command


def main(argv=None, **kwargs):
    return command("list", argv, **kwargs)


if __name__ == "__main__":
    raise SystemExit(main())
