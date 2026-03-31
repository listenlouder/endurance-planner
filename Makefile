.PHONY: css css-watch collectstatic

# Detect OS for binary selection
UNAME := $(shell uname)
ifeq ($(UNAME), Darwin)
  TW = ./bin/tailwindcss-macos
else
  TW = ./bin/tailwindcss
endif

css:
	$(TW) -i backend/static/css/tailwind.css \
	      -o backend/static/css/output.css \
	      --minify

css-watch:
	$(TW) -i backend/static/css/tailwind.css \
	      -o backend/static/css/output.css \
	      --watch

collectstatic:
	cd backend && python manage.py collectstatic --noinput
