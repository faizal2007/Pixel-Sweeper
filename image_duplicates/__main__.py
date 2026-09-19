"""Allow ``python -m image_duplicates [folder]``."""

from image_duplicates.gallery import main

if __name__ == "__main__":
    raise SystemExit(main())