#!/usr/bin/env bash
# Сборка и публикация образа в реестр.
#
#   docker login
#   tools/publish-image.sh <namespace> [тег]
#
# По умолчанию собирается сразу под amd64 и arm64: жюри может открыть проект на ноутбуке
# с Apple Silicon. Список платформ переопределяется переменной CONSTELLATION_PLATFORMS —
# сборка под arm64 на amd64-хосте требует зарегистрированного эмулятора
# (`docker run --privileged --rm tonistiigi/binfmt --install arm64`) и нескольких ГБ на диске;
# без них остаётся `CONSTELLATION_PLATFORMS=linux/amd64`.
set -euo pipefail

NS="${1:?укажите namespace реестра, например oligovit6}"
TAG="${2:-$(git -C "$(dirname "$0")/.." describe --tags --always --dirty 2>/dev/null || echo latest)}"
IMAGE="${NS}/constellation"
PLATFORMS="${CONSTELLATION_PLATFORMS:-linux/amd64,linux/arm64}"
cd "$(dirname "$0")/.."

echo "Собираю ${IMAGE}:${TAG} и ${IMAGE}:latest под ${PLATFORMS}"

# Сборка под несколько архитектур требует отдельного билдера: драйвер docker по умолчанию
# умеет только родную платформу хоста.
docker buildx inspect constellation-builder >/dev/null 2>&1 \
  || docker buildx create --name constellation-builder --driver docker-container --use
docker buildx use constellation-builder

docker buildx build \
  --platform "${PLATFORMS}" \
  --tag "${IMAGE}:${TAG}" \
  --tag "${IMAGE}:latest" \
  --push \
  .

echo
echo "Готово. Проверить запуск из реестра:"
echo "  CONSTELLATION_IMAGE=${IMAGE}:latest docker compose -f docker-compose.hub.yml up -d"
