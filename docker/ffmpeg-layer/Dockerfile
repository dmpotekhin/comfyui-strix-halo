# H3 Studio: тонкий слой поверх уже собранного H3-образа с ComfyUI v0.44.0.
#
# Зачем: в базовом образе gfx1151 нет ffmpeg, а нода CSGlide Video не может
# закодировать видео без него ("no ffmpeg encoders detected ... encoding will fail").
# Раньше бинарник копировали вручную в /usr/local/bin контейнера — это терялось
# при каждом пересоздании контейнера.
#
# Слой ничего не пересобирает: ComfyUI, ROCm и PyTorch остаются как в базовом
# теге. Ставится только ffmpeg из репозитория Ubuntu 24.04 (noble) и сразу
# проверяются нужные кодеки. Тег результата: comfyui-gfx1151:h3-v0.44.1
#
# Сборка:
#   docker build -f docker/ffmpeg-layer/Dockerfile -t comfyui-gfx1151:h3-v0.44.1 docker/ffmpeg-layer
FROM comfyui-gfx1151:h3-v0.44.0

USER root

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && echo "ffmpeg: $(ffmpeg -hide_banner -version | head -n1)" \
 && ffmpeg -hide_banner -encoders | grep -q ' libx264 ' \
 && ffmpeg -hide_banner -encoders | grep -q ' libx265 ' \
 && ffmpeg -hide_banner -encoders | grep -qE ' aac ' \
 && echo "codecs ok: libx264 libx265 aac"
