FROM nextcloud:fpm-alpine

RUN set -eux; \
	apk add --no-cache ffmpeg;

COPY php-fpm-pool.conf /usr/local/etc/php-fpm.d/zz-tuning.conf
