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
// it rejects, so the request silently never works. The 404 arrives as a typed
// result, not an error — the client models that status.
func TestUserIDPathHasNoDecimals(t *testing.T) {
	t.Parallel()

	var path string
	client := newClient(t, http.StatusNotFound, `{"timestamp":"2026-10-13T10:00:00Z","path":"/api/users/12345","message":"User not found","errorCode":"A025"}`, &path)

	res, err := client.UsersControllerGetUserById(t.Context(), api.UsersControllerGetUserByIdParams{UserId: 12345})
	if err != nil {
		t.Fatalf("404 is a modelled status, want a typed result, got error: %v", err)
	}
	if _, ok := res.(*api.RemnawaveNotFoundErrorDto); !ok {
		t.Errorf("result = %T, want *api.RemnawaveNotFoundErrorDto", res)
	}
	if want := "/api/users/12345"; path != want {
		t.Errorf("request path = %q, want %q", path, want)
	}
}

// TestBadRequestDecodesBothErrorShapes covers the flattening done in
// tools/trim-spec.py. The panel answers business failures
// ({timestamp, path, message, errorCode}) and validation failures
// ({message, statusCode, errors}) with the same status code, and the
// specification offers them as an untyped oneOf that ogen cannot generate.
// Flattened into one schema, both must decode — otherwise a routine 400 turns
// into an opaque decode error and the operator loses the message.
//
// The issue fields below are the ones the specification declares required for a
// validation entry; a body that drops them would fail to decode, which is the
// vendor's strictness, kept on purpose.
func TestBadRequestDecodesBothErrorShapes(t *testing.T) {
	t.Parallel()

	t.Run("business error", func(t *testing.T) {
		t.Parallel()
		const body = `{"timestamp":"2026-10-13T10:00:00Z","path":"/api/users","message":"занято","errorCode":"USER_EXISTS"}`
		client := newClient(t, http.StatusBadRequest, body, new(string))

		res, err := client.UsersControllerCreateUser(t.Context(), &api.CreateUserBodyDto{
			Username: "rwx-1", ExpireAt: "2026-10-13T10:00:00.000Z",
		})
		if err != nil {
			t.Fatalf("want a typed result, got error: %v", err)
		}
		bad, ok := res.(*api.RwxUnifiedRemnawaveBadRequestErrorDto)
		if !ok {
			t.Fatalf("result = %T, want *api.RwxUnifiedRemnawaveBadRequestErrorDto", res)
		}
		if got := string(bad.Message); got != "занято" {
			t.Errorf("message = %q, want %q", got, "занято")
		}
		if got := bad.ErrorCode.Value; got != "USER_EXISTS" {
			t.Errorf("errorCode = %q, want %q", got, "USER_EXISTS")
		}
	})

	t.Run("validation error", func(t *testing.T) {
		t.Parallel()
		const body = `{"message":"Bad request","statusCode":400,"errors":[{"validation":"uuid","code":"invalid_string","message":"Invalid uuid","path":["uuid"]}]}`
		client := newClient(t, http.StatusBadRequest, body, new(string))

		res, err := client.UsersControllerCreateUser(t.Context(), &api.CreateUserBodyDto{
			Username: "rwx-1", ExpireAt: "2026-10-13T10:00:00.000Z",
		})
		if err != nil {
			t.Fatalf("want a typed result, got error: %v", err)
		}
		bad, ok := res.(*api.RwxUnifiedRemnawaveBadRequestErrorDto)
		if !ok {
			t.Fatalf("result = %T, want *api.RwxUnifiedRemnawaveBadRequestErrorDto", res)
		}
		if len(bad.Errors) != 1 {
			t.Fatalf("errors = %d, want 1", len(bad.Errors))
		}
		if got := bad.Errors[0].Path; len(got) != 1 || got[0] != "uuid" {
			t.Errorf("errors[0].path = %v, want [uuid]", got)
		}
	})
}

// TestDeviceWithoutOptionalFields covers the nullability restored in
// tools/trim-spec.py. A VPN client that reports no model and no platform is
// ordinary, and the vendor's own 3.4.4 specification marks these fields
// nullable; 3.4.5 makes them required, which would turn one incomplete device
// into a failed decode of the whole list.
func TestDeviceWithoutOptionalFields(t *testing.T) {
	t.Parallel()

	const body = `{"response":{"total":1,"devices":[{"hwid":"device-1","userId":12345,"createdAt":"2026-10-13T10:00:00Z","updatedAt":"2026-10-13T10:05:00Z"}]}}`
	client := newClient(t, http.StatusOK, body, new(string))

	res, err := client.HwidUserDevicesControllerGetUserHwidDevices(t.Context(), api.HwidUserDevicesControllerGetUserHwidDevicesParams{UserId: 12345})
	if err != nil {
		t.Fatalf("want a typed result, got error: %v", err)
	}
	list, ok := res.(*api.GetUserHwidDevicesResponseDto)
	if !ok {
		t.Fatalf("result = %T, want *api.GetUserHwidDevicesResponseDto", res)
	}
	if len(list.Response.Devices) != 1 {
		t.Fatalf("devices = %d, want 1", len(list.Response.Devices))
	}
	device := list.Response.Devices[0]
	if device.Hwid != "device-1" {
		t.Errorf("hwid = %q, want %q", device.Hwid, "device-1")
	}
	if device.Platform.Set {
		t.Errorf("platform = %+v, want unset", device.Platform)
	}
}

// TestUnexpectedStatusStaysReachable covers statuses the specification does not
// model — 503 here, the proxy case rwx-store retries on. The panel can answer
// with something the document never promised, and both the status code and the
// body still have to reach the caller instead of vanishing into a decode failure.
func TestUnexpectedStatusStaysReachable(t *testing.T) {
	t.Parallel()

	client := newClient(t, http.StatusServiceUnavailable, `{"message":"панель недоступна"}`, new(string))

	_, err := client.UsersControllerGetUserById(t.Context(), api.UsersControllerGetUserByIdParams{UserId: 12345})
	if err == nil {
		t.Fatal("want an error from an unmodelled 503 response, got nil")
	}
	var unexpected *validate.UnexpectedStatusCodeError
	if !errors.As(err, &unexpected) {
		t.Fatalf("error %v (%T) does not carry *validate.UnexpectedStatusCodeError", err, err)
	}
	if unexpected.StatusCode != http.StatusServiceUnavailable {
		t.Errorf("StatusCode = %d, want %d", unexpected.StatusCode, http.StatusServiceUnavailable)
	}
}
