# Panel version this client is generated from. The release tag matches it, so
# `git tag` says which panel a checkout is meant to talk to.
SPEC_VERSION := 3.4.5

# Pinned: a different ogen release can produce different code from the same
# specification, and `make check` would then report a diff that has nothing to
# do with the panel.
OGEN_VERSION := v1.24.0

FULL_SPEC    := openapi-$(SPEC_VERSION).json
TRIMMED_SPEC := openapi-trimmed.json
GEN_DIR      := api

.PHONY: generate check test

generate: $(TRIMMED_SPEC)
	go run github.com/ogen-go/ogen/cmd/ogen@$(OGEN_VERSION) \
		--config ogen.json --clean --package api --target $(GEN_DIR) $(TRIMMED_SPEC)

$(TRIMMED_SPEC): tools/trim-spec.py $(FULL_SPEC)
	python3 tools/trim-spec.py $(FULL_SPEC) $@

# Regenerate everything and fail on any difference: the committed client has to
# be exactly what the pinned specification produces, not what someone edited.
check: generate
	git diff --exit-code -- $(GEN_DIR) $(TRIMMED_SPEC)

test:
	go build ./...
	go vet ./...
	go test ./...