.PHONY: help demo test doctor backtest propsim sweep lint clean

help:
	@echo "ABUTRON QUANT"
	@echo "  make demo      # dashboard + paper engine  -> http://localhost:8000"
	@echo "  make test      # full test suite (stdlib unittest)"
	@echo "  make doctor    # readiness / integration checks"
	@echo "  make backtest  # 4k-bar backtest with the prop guard"
	@echo "  make propsim   # prop-challenge pass-rate simulation"
	@echo "  make sweep     # walk-forward parameter sweep"
	@echo "  make clean     # remove runtime artifacts"

demo:
	python3 -m apex serve --demo --port 8000 --preload 1200 --speed 0.8

test:
	python3 -m unittest discover -s tests -v

doctor:
	python3 -m apex doctor

backtest:
	python3 -m apex backtest --symbols R_25,R_50,R_75 --bars 4000 --prop abutron_inhouse --out runs/bt

propsim:
	python3 -m apex propsim --profile ftmo_2step --seeds 12 --bars 4000

sweep:
	python3 -m apex sweep --bars 2500 --seeds 3 --combos 36 --out runs/sweep

lint:
	ruff check apex tests || true

clean:
	rm -rf runs/*.db runs/live runs/bt runs/sweep runs/propsim
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
