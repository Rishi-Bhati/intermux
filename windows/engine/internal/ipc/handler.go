package ipc

import (
	"fmt"
	"os"
	"strings"

	"github.com/Rishi-Bhati/intermux/windows/engine/internal/engine"
)

// Handler dispatches validated JSON v1 requests to the Engine.
type Handler struct {
	eng        *engine.Engine
	exePath    string
	onShutdown func()
}

func NewHandler(eng *engine.Engine, exePath string, onShutdown func()) *Handler {
	return &Handler{eng: eng, exePath: exePath, onShutdown: onShutdown}
}

func mapEngineErr(err error) Response {
	msg := err.Error()
	prefixes := []string{
		"invalid_request", "permission_denied", "not_found", "conflict",
		"not_bindable", "no_slot", "already_adopted",
	}
	for _, p := range prefixes {
		if strings.HasPrefix(msg, p+": ") {
			return ErrorResponse(p, strings.TrimPrefix(msg, p+": "), nil)
		}
	}
	return ErrorResponse("internal", msg, nil)
}

func strParam(params map[string]any, key string) string {
	v, _ := params[key].(string)
	return v
}

func boolParam(params map[string]any, key string) bool {
	v, _ := params[key].(bool)
	return v
}

// Handle processes a raw JSON request line in the security context of callerSID.
func (h *Handler) Handle(raw []byte, callerSID string) Response {
	req, err := ParseRequest(raw)
	if err != nil {
		return ErrorResponse("invalid_request", err.Error(), nil)
	}

	switch req.Action {
	case "ping":
		return OkResponse(map[string]any{
			"pong":     true,
			"version":  engine.Version,
			"mode":     h.eng.Mode(),
			"pid":      os.Getpid(),
			"uptime_s": h.eng.UptimeSec(),
		})

	case "interfaces":
		list, err := h.eng.ListInterfaces(boolParam(req.Params, "include_all"))
		if err != nil {
			return mapEngineErr(err)
		}
		return OkResponse(map[string]any{"interfaces": list})

	case "status":
		return OkResponse(h.eng.Status())

	case "bind_register":
		iface, err := ValidateIfaceName(strParam(req.Params, "iface"))
		if err != nil {
			return ErrorResponse("invalid_request", err.Error(), nil)
		}
		app := strParam(req.Params, "app")
		if app == "" {
			return ErrorResponse("invalid_request", "app is required", nil)
		}
		pids, err := getUint32Slice(req.Params, "pids")
		if err != nil {
			return ErrorResponse("invalid_request", err.Error(), nil)
		}
		dnsSpec, err := parseDNSParam(req.Params["dns"], true)
		if err != nil {
			return ErrorResponse("invalid_request", err.Error(), nil)
		}
		var downLim, upLim *float64
		if rawLim, ok := req.Params["limit"].(map[string]any); ok {
			downLim, _, err = getOptionalFloatPtr(rawLim, "down")
			if err != nil {
				return ErrorResponse("invalid_request", err.Error(), nil)
			}
			upLim, _, err = getOptionalFloatPtr(rawLim, "up")
			if err != nil {
				return ErrorResponse("invalid_request", err.Error(), nil)
			}
		}
		res, err := h.eng.BindRegister(
			strParam(req.Params, "id"),
			iface,
			app,
			pids,
			callerSID,
			dnsSpec,
			downLim,
			upLim,
			boolParam(req.Params, "socks"),
		)
		if err != nil {
			return mapEngineErr(err)
		}
		return OkResponse(res)

	case "bind_unregister":
		id := strParam(req.Params, "id")
		iface := strParam(req.Params, "iface")
		app := strParam(req.Params, "app")
		if id == "" && (iface == "" || app == "") {
			return ErrorResponse("invalid_request", "provide id or iface+app", nil)
		}
		res, err := h.eng.BindUnregister(id, iface, app, callerSID)
		if err != nil {
			return mapEngineErr(err)
		}
		return OkResponse(res)

	case "adopt_pids":
		iface, err := ValidateIfaceName(strParam(req.Params, "iface"))
		if err != nil {
			return ErrorResponse("invalid_request", err.Error(), nil)
		}
		app := strParam(req.Params, "app")
		if app == "" {
			return ErrorResponse("invalid_request", "app is required", nil)
		}
		id := strParam(req.Params, "id")
		if id != "" && !adoptIDRe.MatchString(id) {
			return ErrorResponse("invalid_request", "invalid adoption id", nil)
		}
		pids, err := getUint32Slice(req.Params, "pids")
		if err != nil || len(pids) == 0 {
			return ErrorResponse("invalid_request", "pids must be a non-empty list of integers", nil)
		}
		res, err := h.eng.AdoptPIDs(id, iface, app, pids, callerSID)
		if err != nil {
			return mapEngineErr(err)
		}
		return OkResponse(res)

	case "release_adoption":
		id := strParam(req.Params, "id")
		if !adoptIDRe.MatchString(id) {
			return ErrorResponse("invalid_request", "invalid adoption id", nil)
		}
		res, err := h.eng.ReleaseAdoption(id, callerSID)
		if err != nil {
			return mapEngineErr(err)
		}
		return OkResponse(res)

	case "set_dns":
		iface, err := ValidateIfaceName(strParam(req.Params, "iface"))
		if err != nil {
			return ErrorResponse("invalid_request", err.Error(), nil)
		}
		dnsSpec, err := parseDNSParam(req.Params["dns"], false)
		if err != nil {
			return ErrorResponse("invalid_request", err.Error(), nil)
		}
		res, err := h.eng.SetDNS(iface, dnsSpec)
		if err != nil {
			return mapEngineErr(err)
		}
		return OkResponse(res)

	case "set_limit":
		iface, err := ValidateIfaceName(strParam(req.Params, "iface"))
		if err != nil {
			return ErrorResponse("invalid_request", err.Error(), nil)
		}
		down, _, err := getOptionalFloatPtr(req.Params, "down", "down_mbit")
		if err != nil {
			return ErrorResponse("invalid_request", err.Error(), nil)
		}
		up, _, err := getOptionalFloatPtr(req.Params, "up", "up_mbit")
		if err != nil {
			return ErrorResponse("invalid_request", err.Error(), nil)
		}
		res, err := h.eng.SetLimit(iface, down, up)
		if err != nil {
			return mapEngineErr(err)
		}
		return OkResponse(res)

	case "counters":
		ifaces, _ := strSliceParam(req.Params, "ifaces")
		bindings, _ := strSliceParam(req.Params, "bindings")
		pids, err := getUint32Slice(req.Params, "pids")
		if err != nil {
			return ErrorResponse("invalid_request", err.Error(), nil)
		}
		return OkResponse(h.eng.Counters(ifaces, bindings, pids))

	case "verify":
		iface, err := ValidateIfaceName(strParam(req.Params, "iface"))
		if err != nil {
			return ErrorResponse("invalid_request", err.Error(), nil)
		}
		res, err := h.eng.Verify(iface)
		if err != nil {
			return mapEngineErr(err)
		}
		return OkResponse(res)

	case "reconnect":
		res, err := h.eng.Reconnect()
		if err != nil {
			return mapEngineErr(err)
		}
		return OkResponse(res)

	case "teardown_idle":
		return OkResponse(h.eng.TeardownIdle())

	case "reset":
		return OkResponse(h.eng.Reset(boolParam(req.Params, "kill_apps"), callerSID))

	case "passwordless_enable":
		if callerSID == "" {
			return ErrorResponse("permission_denied", "caller SID unavailable", nil)
		}
		if err := InstallPasswordlessTask(h.exePath, callerSID); err != nil {
			return ErrorResponse("command_failed", err.Error(), nil)
		}
		return OkResponse(map[string]any{"enabled": true, "task": TaskNameForSID(callerSID)})

	case "passwordless_disable":
		if callerSID == "" {
			return ErrorResponse("permission_denied", "caller SID unavailable", nil)
		}
		if err := RemovePasswordlessTask(callerSID); err != nil {
			return ErrorResponse("command_failed", err.Error(), nil)
		}
		return OkResponse(map[string]any{"enabled": false, "task": TaskNameForSID(callerSID)})

	case "shutdown":
		if h.onShutdown != nil {
			go h.onShutdown()
		}
		return OkResponse(map[string]any{"shutting_down": true})

	default:
		return ErrorResponse("unsupported_action", fmt.Sprintf("unknown action: %s", req.Action), nil)
	}
}

func strSliceParam(params map[string]any, key string) ([]string, bool) {
	raw, ok := params[key].([]any)
	if !ok {
		return nil, false
	}
	out := make([]string, 0, len(raw))
	for _, item := range raw {
		if s, ok := item.(string); ok {
			out = append(out, s)
		}
	}
	return out, true
}
