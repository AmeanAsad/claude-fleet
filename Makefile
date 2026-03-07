.PHONY: install reinstall

install:
	uv pip install -e .

reinstall:
	uv pip install --no-cache -e . --force-reinstall
