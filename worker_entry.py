"""Frozen audio worker; the desktop application owns its stdin lifetime."""
from sender import main

if __name__ == "__main__":
    main()
