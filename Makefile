# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

.PHONY: tests dashboard-e2e dashboard-e2e-nobuild dashboard-e2e-install dashboard-e2e-hardware

PYTHON_PATH := $(shell which python)

# If uv is installed and a virtual environment exists, use it
UV_CHECK := $(shell command -v uv)
ifneq ($(UV_CHECK),)
	PYTHON_PATH := $(shell .venv/bin/python)
endif

export PATH := $(dir $(PYTHON_PATH)):$(PATH)

DEVICE ?= cpu

build-user:
	docker build -f docker/Dockerfile.user -t lerobot-user .

# Dashboard E2E suite (Playwright + gemini-vision visual QA).
# Spawns `uv run lerobot-dashboard` internally; requires the 'dashboard' extra installed.
# Depends on 'dashboard-frontend' so the Vite bundle exists before Playwright boots
# the server with --static-dir src/lerobot/dashboard/static.
#
# Vite's emptyOutDir wipes static/.gitignore and static/.gitkeep during build;
# restore them so the worktree stays clean after install. (Follow-up: frontend
# team to switch to emptyOutDir=false or vite-plugin-static-copy.)
dashboard-e2e-install: dashboard-frontend
	@git checkout HEAD -- src/lerobot/dashboard/static/.gitignore src/lerobot/dashboard/static/.gitkeep 2>/dev/null || true
	cd tests/dashboard/e2e && npm install && npx playwright install --with-deps chromium

# Default CI target — rebuilds the Vite bundle before running Playwright so the
# dashboard server is guaranteed to serve the latest frontend. The static
# .gitignore/.gitkeep pair is restored after the build (see install target).
# Use ``dashboard-e2e-nobuild`` for the fast dev-inner-loop when you know
# the bundle is fresh.
dashboard-e2e: dashboard-frontend
	@git checkout HEAD -- src/lerobot/dashboard/static/.gitignore src/lerobot/dashboard/static/.gitkeep 2>/dev/null || true
	cd tests/dashboard/e2e && npm test

# Fast inner-loop variant: skip the Vite rebuild. Use when only spec files
# or backend code changed; run 'make dashboard-e2e' after any frontend edit.
dashboard-e2e-nobuild:
	cd tests/dashboard/e2e && npm test

# Hardware E2E smoke: drives real attached robots via pytest (no browser).
# Requires: SO-101 connected at /dev/ttyACM0, lerobot[feetech] extra installed.
# Tests are marked @pytest.mark.hardware and skip cleanly when hardware absent.
dashboard-e2e-hardware:
	uv run pytest tests/dashboard/e2e/hardware/ -m hardware -v

build-internal:
	docker build -f docker/Dockerfile.internal -t lerobot-internal .

test-end-to-end:
	${MAKE} DEVICE=$(DEVICE) test-act-ete-train
	${MAKE} DEVICE=$(DEVICE) test-act-ete-train-resume
	${MAKE} DEVICE=$(DEVICE) test-act-ete-eval
	${MAKE} DEVICE=$(DEVICE) test-diffusion-ete-train
	${MAKE} DEVICE=$(DEVICE) test-diffusion-ete-eval
	${MAKE} DEVICE=$(DEVICE) test-tdmpc-ete-train
	${MAKE} DEVICE=$(DEVICE) test-tdmpc-ete-eval
	${MAKE} DEVICE=$(DEVICE) test-smolvla-ete-train
	${MAKE} DEVICE=$(DEVICE) test-smolvla-ete-eval

test-act-ete-train:
	lerobot-train \
		--policy.type=act \
		--policy.dim_model=64 \
		--policy.n_action_steps=20 \
		--policy.chunk_size=20 \
		--policy.device=$(DEVICE) \
		--policy.push_to_hub=false \
		--env.type=aloha \
		--env.episode_length=5 \
		--dataset.repo_id=lerobot/aloha_sim_transfer_cube_human \
		--dataset.image_transforms.enable=true \
		--dataset.episodes="[0]" \
		--batch_size=2 \
		--steps=4 \
		--eval_freq=2 \
		--eval.n_episodes=1 \
		--eval.batch_size=1 \
		--save_freq=2 \
		--save_checkpoint=true \
		--log_freq=1 \
		--wandb.enable=false \
		--output_dir=tests/outputs/act/

test-act-ete-train-resume:
	lerobot-train \
		--config_path=tests/outputs/act/checkpoints/000002/pretrained_model/train_config.json \
		--resume=true

test-act-ete-eval:
	lerobot-eval \
		--policy.path=tests/outputs/act/checkpoints/000004/pretrained_model \
		--policy.device=$(DEVICE) \
		--env.type=aloha \
		--env.episode_length=5 \
		--eval.n_episodes=1 \
		--eval.batch_size=1

test-diffusion-ete-train:
	lerobot-train \
		--policy.type=diffusion \
		--policy.down_dims='[64,128,256]' \
		--policy.diffusion_step_embed_dim=32 \
		--policy.num_inference_steps=10 \
		--policy.device=$(DEVICE) \
		--policy.push_to_hub=false \
		--env.type=pusht \
		--env.episode_length=5 \
		--dataset.repo_id=lerobot/pusht \
		--dataset.image_transforms.enable=true \
		--dataset.episodes="[0]" \
		--batch_size=2 \
		--steps=2 \
		--eval_freq=2 \
		--eval.n_episodes=1 \
		--eval.batch_size=1 \
		--save_checkpoint=true \
		--save_freq=2 \
		--log_freq=1 \
		--wandb.enable=false \
		--output_dir=tests/outputs/diffusion/

test-diffusion-ete-eval:
	lerobot-eval \
		--policy.path=tests/outputs/diffusion/checkpoints/000002/pretrained_model \
		--policy.device=$(DEVICE) \
		--env.type=pusht \
		--env.episode_length=5 \
		--eval.n_episodes=1 \
		--eval.batch_size=1

test-tdmpc-ete-train:
	lerobot-train \
		--policy.type=tdmpc \
		--policy.device=$(DEVICE) \
		--policy.push_to_hub=false \
		--env.type=pusht \
		--env.episode_length=5 \
		--dataset.repo_id=lerobot/pusht_image \
		--dataset.image_transforms.enable=true \
		--dataset.episodes="[0]" \
		--batch_size=2 \
		--steps=2 \
		--eval_freq=2 \
		--eval.n_episodes=1 \
		--eval.batch_size=1 \
		--save_checkpoint=true \
		--save_freq=2 \
		--log_freq=1 \
		--wandb.enable=false \
		--output_dir=tests/outputs/tdmpc/

test-tdmpc-ete-eval:
	lerobot-eval \
		--policy.path=tests/outputs/tdmpc/checkpoints/000002/pretrained_model \
		--policy.device=$(DEVICE) \
		--env.type=pusht \
		--env.episode_length=5 \
		--env.observation_height=96 \
        --env.observation_width=96 \
		--eval.n_episodes=1 \
		--eval.batch_size=1


test-smolvla-ete-train:
	lerobot-train \
		--policy.type=smolvla \
		--policy.n_action_steps=20 \
		--policy.chunk_size=20 \
		--policy.device=$(DEVICE) \
		--policy.push_to_hub=false \
		--env.type=aloha \
		--env.episode_length=5 \
		--dataset.repo_id=lerobot/aloha_sim_transfer_cube_human \
		--dataset.image_transforms.enable=true \
		--dataset.episodes="[0]" \
		--batch_size=2 \
		--steps=4 \
		--eval_freq=2 \
		--eval.n_episodes=1 \
		--eval.batch_size=1 \
		--save_freq=2 \
		--save_checkpoint=true \
		--log_freq=1 \
		--wandb.enable=false \
		--output_dir=tests/outputs/smolvla/

test-smolvla-ete-eval:
	lerobot-eval \
		--policy.path=tests/outputs/smolvla/checkpoints/000004/pretrained_model \
		--policy.device=$(DEVICE) \
		--env.type=aloha \
		--env.episode_length=5 \
		--eval.n_episodes=1 \
		--eval.batch_size=1

# --- Dashboard frontend ---
DASHBOARD_FRONTEND_DIR := src/lerobot/dashboard/frontend

.PHONY: dashboard-frontend dashboard-frontend-dev dashboard-frontend-install

dashboard-frontend-install:
	cd $(DASHBOARD_FRONTEND_DIR) && npm ci

dashboard-frontend:
	cd $(DASHBOARD_FRONTEND_DIR) && npm ci && npm run build

dashboard-frontend-dev:
	cd $(DASHBOARD_FRONTEND_DIR) && npm run dev
