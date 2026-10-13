// Package api_test pins the two behaviours this client had to be corrected for.
//
// The panel's published specification (3.4.5) types identifier parameters as
// `number` and declares every error body as an untyped `oneOf`. Both shapes are
// corrected or dropped in tools/trim-spec.py, and both corrections are easy to
// undo by regenerating. These tests fail loudly if that happens, because the
// failure is invisible otherwise: a float in a path is only noticed when the
// panel rejects the request, and a missing error body only when a human reads
// an unhelpful log line.
package api_test

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/ogen-go/ogen/validate"
	api "github.com/rwx-core/remnawave-api-go/v3/api"
)

type staticAuth struct{}

func (staticAuth) Authorization(context.Context, api.OperationName) (api.Authorization, error) {
	return api.Authorization{Token: "test-token"}, nil
}

// newClient returns a client pointed at a server that records the request path
// and then answers with the given status and body.
func newClient(t *testing.T, status int, body string, seenPath *string) *api.Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*seenPath = r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}))
	t.Cleanup(srv.Close)

	client, err := api.NewClient(srv.URL, staticAuth{})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	return client
}

// TestUserIDPathHasNoDecimals guards the vendor's `type: number` mistake: an
// identifier rendered as a float reaches the panel as `12345.0000000000`, which
// it rejects, so the request silently never works.
func TestUserIDPathHasNoDecimals(t *testing.T) {
	t.Parallel()

	var path string
	client := newClient(t, http.StatusNotFound, `{}`, &path)

	_, err := client.UsersControllerGetUserById(t.Context(), api.UsersControllerGetUserByIdParams{UserId: 12345})
	if err == nil {
		t.Fatal("want an error from a 404 response, got nil")
	}
	if want := "/api/users/12345"; path != want {
		t.Errorf("request path = %q, want %q", path, want)
	}
}

// TestErrorBodyStaysReachable documents the boundary this client leaves to its
// callers: error bodies are not modelled, so a non-2xx answer arrives as an
// *validate.UnexpectedStatusCodeError whose payload still carries the body.
// Callers decode it against the published specification themselves.
func TestErrorBodyStaysReachable(t *testing.T) {
	t.Parallel()

	const want = `{"message":"плохой запрос","errors":[{"path":["username"],"message":"занято"}]}`
	client := newClient(t, http.StatusBadRequest, want, new(string))

	_, err := client.UsersControllerGetUserById(t.Context(), api.UsersControllerGetUserByIdParams{UserId: 12345})
	if err == nil {
		t.Fatal("want an error from a 400 response, got nil")
	}

	var unexpected *validate.UnexpectedStatusCodeError
	if !errors.As(err, &unexpected) {
		t.Fatalf("error %v (%T) does not carry *validate.UnexpectedStatusCodeError", err, err)
	}
	if unexpected.StatusCode != http.StatusBadRequest {
		t.Errorf("StatusCode = %d, want %d", unexpected.StatusCode, http.StatusBadRequest)
	}
	body, readErr := io.ReadAll(unexpected.Payload.Body)
	if readErr != nil {
		t.Fatalf("read error body: %v", readErr)
	}
	if string(body) != want {
		t.Errorf("body = %s, want %s", body, want)
	}
}
