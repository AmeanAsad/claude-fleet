.PHONY: install reinstall

install:
	uv tool install -e . --python 3.11

reinstall:
	uv tool install -e . --python 3.11 --force-reinstall
