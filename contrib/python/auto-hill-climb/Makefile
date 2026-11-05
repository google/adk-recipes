# Every target grades with agents-cli. Set GOOGLE_CLOUD_PROJECT in .env or your shell first.
-include .env
export
PROJECT ?= $(GOOGLE_CLOUD_PROJECT)
CONFIG := tests/eval/.eval_config.local.yaml
ROUND ?= candidate
A ?= baseline
B ?= $(ROUND)

.PHONY: config baseline round heldout compare

config:
	@test -n "$(PROJECT)" || (echo "Set GOOGLE_CLOUD_PROJECT to your Google Cloud project id" && exit 1)
	@sed "s/YOUR_PROJECT_ID/$(PROJECT)/" tests/eval/eval_config.yaml > $(CONFIG)

baseline: config
	agents-cli eval run --dataset tests/eval/datasets/train.json --config $(CONFIG) --output artifacts/grade_results/baseline

round: config
	agents-cli eval run --dataset tests/eval/datasets/train.json --config $(CONFIG) --output artifacts/grade_results/$(ROUND)

heldout: config
	agents-cli eval run --dataset tests/eval/datasets/heldout.json --config $(CONFIG) --output artifacts/grade_results/heldout

compare:
	agents-cli eval compare artifacts/grade_results/$(A) artifacts/grade_results/$(B)
