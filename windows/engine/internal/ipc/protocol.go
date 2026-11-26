package ipc

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"

	"github.com/Rishi-Bhati/intermux/windows/engine/internal/dns"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/shaping"
)

const (
	ProtocolVersion = 1
	MaxRequestBytes = 1024 * 1024
)

var (
	adoptIDRe = regexp.MustCompile(`^[0-9a-f]{8,32}$`)
	validActions = map[string]bool{
		"ping":                 true,
		"interfaces":           true,
		"status":               true,
		"bind_register":        true,
		"bind_unregister":      true,
		"adopt_pids":           true,
		"release_adoption":     true,
		"set_dns":              true,
		"set_limit":            true,
		"counters":             true,
		"verify":               true,
		"reconnect":            true,
		"teardown_idle":        true,
		"reset":                true,
		"shutdown":             true,
		"passwordless_enable":  true,
		"passwordless_disable": true,
	}
)

type Request struct {
	V      int            `json:"v"`
	Action string         `json:"action"`
	Params map[string]any `json:"params"`
}

type ErrorBody struct {
	Code    string `json:"code"`
	Message string `json:"message"`
	Data    any    `json:"data"`
}

type Response struct {
	OK    bool       `json:"ok"`
	Data  any        `json:"data,omitempty"`
	Error *ErrorBody `json:"error,omitempty"`
}

func OkResponse(data any) Response {
	return Response{OK: true, Data: data}
}

func ErrorResponse(code, message string, data any) Response {
	return Response{OK: false, Error: &ErrorBody{Code: code, Message: message, Data: data}}
}

func ValidateIfaceName(iface string) (string, error) {
	if iface == "" || strings.TrimSpace(iface) == "" || len(iface) > 256 {
		return "", errors.New("invalid interface name")
	}
	if iface == "." || iface == ".." || strings.Contains(iface, "..") ||
		strings.ContainsAny(iface, "/\\") {
		return "", errors.New("invalid interface name: path separators or '..' not allowed")
	}
	for i := 0; i < len(iface); i++ {
		if iface[i] < 0x20 || iface[i] == 0x7f {
			return "", errors.New("invalid interface name: control characters not allowed")
		}
	}
	return iface, nil
}

// ParseRequest strictly validates a JSON v1 request envelope (matching intermux/helper/protocol.py).
func ParseRequest(raw []byte) (Request, error) {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 {
		return Request{}, errors.New("empty request")
	}
	if len(trimmed) > MaxRequestBytes {
		return Request{}, errors.New("request exceeds 1 MiB limit")
	}

	var top map[string]json.RawMessage
	if err := json.Unmarshal(trimmed, &top); err != nil {
		return Request{}, fmt.Errorf("invalid JSON: %w", err)
	}
	for k := range top {
		if k != "v" && k != "action" && k != "params" {
			return Request{}, fmt.Errorf("unknown request field: %s", k)
		}
	}

	var ver int
	vRaw, ok := top["v"]
	if !ok || json.Unmarshal(vRaw, &ver) != nil || ver != ProtocolVersion {
		return Request{}, fmt.Errorf("unsupported protocol version; expected %d", ProtocolVersion)
	}

	var action string
	aRaw, ok := top["action"]
	if !ok || json.Unmarshal(aRaw, &action) != nil || !validActions[action] {
		return Request{}, errors.New("unknown action")
	}

	params := make(map[string]any)
	if pRaw, exists := top["params"]; exists {
		dec := json.NewDecoder(bytes.NewReader(pRaw))
		dec.UseNumber()
		if err := dec.Decode(&params); err != nil || params == nil {
			return Request{}, errors.New("params must be an object")
		}
	}
	return Request{V: ver, Action: action, Params: params}, nil
}

func getOptionalFloatPtr(params map[string]any, keys ...string) (*float64, bool, error) {
	for _, k := range keys {
		val, exists := params[k]
		if !exists {
			continue
		}
		if val == nil {
			return nil, true, nil
		}
		switch t := val.(type) {
		case json.Number:
			f, err := t.Float64()
			if err != nil {
				return nil, true, err
			}
			clean, err := shaping.ValidateMbit(&f)
			return clean, true, err
		case float64:
			clean, err := shaping.ValidateMbit(&t)
			return clean, true, err
		default:
			return nil, true, fmt.Errorf("%s must be a number or null", k)
		}
	}
	return nil, false, nil
}

func getUint32Slice(params map[string]any, key string) ([]uint32, error) {
	raw, exists := params[key]
	if !exists || raw == nil {
		return nil, nil
	}
	arr, ok := raw.([]any)
	if !ok {
		return nil, fmt.Errorf("%s must be an array of integers", key)
	}
	out := make([]uint32, 0, len(arr))
	for _, item := range arr {
		num, ok := item.(json.Number)
		if !ok {
			return nil, fmt.Errorf("%s entries must be integers", key)
		}
		v, err := num.Int64()
		if err != nil || v <= 0 || v > 0xFFFFFFFF {
			return nil, fmt.Errorf("invalid PID in %s: %v", key, item)
		}
		out = append(out, uint32(v))
	}
	return out, nil
}

func parseDNSParam(raw any, allowKeep bool) (any, error) {
	if raw == nil {
		if allowKeep {
			return dns.Keep, nil
		}
		return nil, errors.New("dns is required")
	}
	switch v := raw.(type) {
	case string:
		if v == dns.Auto || (allowKeep && v == dns.Keep) {
			return v, nil
		}
		return nil, fmt.Errorf("invalid dns string %q", v)
	case []any:
		srvs := make([]string, 0, len(v))
		for _, item := range v {
			s, ok := item.(string)
			if !ok {
				return nil, errors.New("dns entries must be IPv4 strings")
			}
			srvs = append(srvs, s)
		}
		return dns.ValidateServers(srvs)
	default:
		return nil, errors.New("dns must be 'auto', 'keep', or list of IPv4 strings")
	}
}
