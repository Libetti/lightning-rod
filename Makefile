.PHONY: clear-cmi-cache

clear-cmi-cache:
	rm -rf "$${TMPDIR:-/tmp}/lightning_rod_cmi"
	@echo "Cleared CMI cache: $${TMPDIR:-/tmp}/lightning_rod_cmi"
