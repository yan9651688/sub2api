package handler

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

func TestCodexBootstrapPreflightPreservesEscapedMembers(t *testing.T) {
	body := []byte(`{"model":"gpt-5","input":[{"type":"function_call_output","namespace":"codex_app","name":"create_thread","output":"` + delegationEnvelope + `"}]}`)
	for _, key := range []string{"input", "type", "name", "function_call_output", "create_thread"} {
		t.Run(key, func(t *testing.T) {
			escaped := []byte(strings.ReplaceAll(string(body), key, fmt.Sprintf(`\u%04x%s`, key[0], key[1:])))
			got, changed := normalizeCodexDelegationBootstrap(escaped)
			want, wantChanged := normalizeCodexCallOutputBootstrap(escaped, isCodexDelegationCandidate, true)
			if !changed || changed != wantChanged || !bytes.Equal(got, want) {
				t.Fatal("escaped candidate differs from original normalization")
			}
		})
	}
}

func TestCodexBootstrapPreflightSkipsLargeOrdinaryHistory(t *testing.T) {
	body := []byte(`{"tools":[{"name":"create_thread"},{"name":"automation_update"}],"input":[{"type":"message","content":"` + strings.Repeat("x", 1<<20) + `"},{"type":"function_call_output","call_id":"call_1","output":"ok"}]}`)
	allocs := testing.AllocsPerRun(5, func() {
		got, changed := normalizeCodexAutomationBootstrap(body)
		if changed || &got[0] != &body[0] {
			t.Fatal("ordinary automation history was copied or changed")
		}
		got, changed = normalizeCodexDelegationBootstrap(body)
		if changed || &got[0] != &body[0] {
			t.Fatal("ordinary delegation history was copied or changed")
		}
	})
	if allocs > 20 {
		t.Fatalf("ordinary request still takes the full JSON decoding path: %.0f allocations", allocs)
	}
}

// Compare the preflight with the unchanged strict implementation, including
// malformed JSON and duplicate/escaped members. False positives are harmless;
// a false negative would silently skip a valid bootstrap conversion.
func FuzzCodexBootstrapPreflightEquivalent(f *testing.F) {
	f.Add([]byte(`{"input":[]}`))
	f.Add([]byte(`{"input":[{"type":"function_call_output","namespace":"codex_app","name":"create_thread","output":"` + delegationEnvelope + `"}]}`))
	heartbeat, _ := json.Marshal(map[string]any{"input": []any{map[string]any{"type": "function_call_output", "namespace": "codex_app", "name": "automation_update", "output": "[Automation heartbeat]\nCheck the current state."}}})
	f.Add(heartbeat)
	f.Add([]byte(`{"input":[],"input":[{"type":"function_call_output","name":"create_thread"}]}`))
	f.Add([]byte(`{"\u0069nput":[{"type":"function_call_output","namespace":"codex_app","name":"create_\u0074hread","output":"` + delegationEnvelope + `"}]}`))
	f.Fuzz(func(t *testing.T, body []byte) {
		if len(body) > 2<<20 {
			t.Skip()
		}
		for _, tc := range []struct {
			name      string
			normalize func([]byte) ([]byte, bool)
			candidate func(map[string]any) bool
			history   bool
		}{
			{"automation", normalizeCodexAutomationBootstrap, isCodexAutomationCandidate, false},
			{"delegation", normalizeCodexDelegationBootstrap, isCodexDelegationCandidate, true},
		} {
			got, changed := tc.normalize(body)
			want, wantChanged := normalizeCodexCallOutputBootstrap(body, tc.candidate, tc.history)
			if changed != wantChanged || !bytes.Equal(got, want) {
				t.Fatalf("%s normalization differs from original", tc.name)
			}
		}
	})
}

// Model the large uncompressed Responses requests seen in the deployment,
// without copying any customer request content into the test suite.
func BenchmarkCodexBootstrapLargeHistory(b *testing.B) {
	for _, size := range []int{1 << 20, 8 << 20, 32 << 20} {
		body := append([]byte(`{"model":"gpt-5","stream":true,"tools":[{"type":"function","name":"create_thread"},{"type":"function","name":"automation_update"}],"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"`), bytes.Repeat([]byte("a"), size)...)
		body = append(body, []byte(`"}]},{"type":"function_call_output","call_id":"call_1","output":"ok"}]}`)...)
		b.Run(fmt.Sprintf("%dMiB", size>>20), func(b *testing.B) {
			b.ReportAllocs()
			b.SetBytes(int64(len(body)))
			for b.Loop() {
				got, changed := normalizeCodexAutomationBootstrap(body)
				if changed || len(got) != len(body) {
					b.Fatal("ordinary history changed")
				}
				got, changed = normalizeCodexDelegationBootstrap(got)
				if changed || len(got) != len(body) {
					b.Fatal("ordinary history changed")
				}
			}
		})
	}
}
