package repository

import (
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"
)

func TestOpsAvailabilityEventFilterUsesExactBoundMessage(t *testing.T) {
	for _, event := range []string{"openai.upstream_failover_switching", "event_%' OR TRUE --"} {
		where, args, constrained := buildOpsSystemLogsWhere(&service.OpsSystemLogFilter{Event: event})
		if !constrained || !strings.Contains(where, "l.message = $1") || len(args) != 1 || args[0] != event {
			t.Fatalf("expected a bound exact event filter, got %q %#v", where, args)
		}
		if strings.Contains(where, event) || strings.Contains(where, "ILIKE") || strings.Contains(where, "extra::text") {
			t.Fatalf("event counts must not match embedded payload text or SQL wildcards: %q", where)
		}
	}
}

func TestOpsAvailabilityEventFilterDoesNotChangeLegacySearch(t *testing.T) {
	where, args, _ := buildOpsSystemLogsWhere(&service.OpsSystemLogFilter{Query: "upstream"})
	if strings.Contains(where, "l.message =") || !strings.Contains(where, "l.message ILIKE") || len(args) != 1 || args[0] != "%upstream%" {
		t.Fatalf("legacy text search changed: %q %#v", where, args)
	}
}
