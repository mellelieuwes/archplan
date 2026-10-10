package main

import (
	"strings"
	"testing"
)

const stepsSample = `[
  {"name": "Create task as editor", "handler": "POST /api/v1/task", "diagram": {
    "participants": ["dataentry", "acl", "store/pgstore"],
    "root": {"from": "Caller", "to": "dataentry", "fn": "handleCreate", "args": ["w=*http.response", "r=*http.Request"], "children": [
      {"from": "dataentry", "to": "acl", "fn": "ResolvePrincipal", "args": ["rawUser=\"bob@example.com\""]},
      {"repeat": 2, "children": [
        {"from": "dataentry", "to": "store/pgstore", "fn": "Get", "args": ["id=\"T-1\""]},
        {"repeat": 3, "children": [
          {"from": "store/pgstore", "to": "store/pgstore", "fn": "scan"}
        ]}
      ]},
      {"from": "dataentry", "to": "store/pgstore", "fn": "Get", "via": "go", "args": ["id=\"T-2\""]}
    ]}
  }},
  {"name": "no diagram"}
]`

func TestSequenceFrom(t *testing.T) {
	scs, err := ParseSteps([]byte(stepsSample))
	if err != nil {
		t.Fatal(err)
	}
	if len(scs) != 2 || scs[1].Diagram != nil {
		t.Fatalf("scenarios = %+v", scs)
	}
	partOf := func(p string) string {
		if p == "store/pgstore" {
			return "pgstore"
		}
		return ""
	}
	s := SequenceFrom(scs[0], partOf)
	if got := strings.Join(s.Participants, ","); got != "Caller,dataentry,acl,pgstore" {
		t.Errorf("participants = %s", got)
	}
	if s.Title != "Create task as editor" || s.Entry != "POST /api/v1/task" || s.Source != "seqtrace" {
		t.Errorf("header = %q %q %q", s.Title, s.Entry, s.Source)
	}
	type row struct {
		id, label, frag string
		depth           int
		async           bool
	}
	want := []row{
		{"Caller>dataentry:handleCreate#1", "handleCreate(w, r)", "", 0, false},
		{"dataentry>acl:ResolvePrincipal#1", "ResolvePrincipal(rawUser)", "", 1, false},
		{"dataentry>pgstore:Get#1", "Get(id)", "L1", 1, false},
		{"pgstore>pgstore:scan#1", "scan()", "L2", 1, false},
		{"dataentry>pgstore:Get#2", "Get(id)", "", 1, true},
	}
	if len(s.Messages) != len(want) {
		t.Fatalf("messages = %+v", s.Messages)
	}
	for i, w := range want {
		m := s.Messages[i]
		if m.ID != w.id || m.Label != w.label || m.Fragment != w.frag || m.Depth != w.depth || m.Async != w.async {
			t.Errorf("message %d = %+v, want %+v", i, m, w)
		}
		if strings.Contains(m.Label, "bob") || strings.Contains(m.Label, "T-") {
			t.Errorf("message %d leaks an argument value: %s", i, m.Label)
		}
	}
	if f := s.Fragments["L1"]; f.Kind != "loop" || f.Label != "×2" || f.Parent != nil {
		t.Errorf("L1 = %+v", f)
	}
	if f := s.Fragments["L2"]; f.Label != "×3" || f.Parent == nil || *f.Parent != "L1" {
		t.Errorf("L2 = %+v", f)
	}
	if id := SequenceID(s.Title); id != "create-task-as-editor" {
		t.Errorf("SequenceID = %q", id)
	}
}

func TestParseStepsRejectsBadJSON(t *testing.T) {
	if _, err := ParseSteps([]byte("{")); err == nil {
		t.Fatal("want an error")
	}
}
