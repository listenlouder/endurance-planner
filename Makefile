.PHONY: css css-watch collectstatic

# Detect OS for binary selection
ifeq ($(OS), Windows_NT)
  TW = .\bin\tailwindcss.exe
else ifeq ($(shell uname), Darwin)
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
