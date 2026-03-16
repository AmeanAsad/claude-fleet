.PHONY: install reinstall

install:
	uv tool install -e .

reinstall:
	uv tool install -e . --force-reinstall
