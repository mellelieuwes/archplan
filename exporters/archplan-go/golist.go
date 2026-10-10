package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os/exec"
	"strings"
)

// Package is the part of `go list -json` output the exporter reads.
type Package struct {
	ImportPath string
	Dir        string
	Name       string
	Standard   bool
	DepOnly    bool
	Export     string
	GoFiles    []string
	CgoFiles   []string
	Module     *struct{ Path string }
	Error      *struct{ Err string }
}

// ListPackages runs `go list -e -json -deps -export` in dir. The result is in
// dependency order: every package comes after the packages it imports.
// -export builds the packages, so the type checker can read compiled export
// data for everything outside the module.
func ListPackages(ctx context.Context, dir, tags string, patterns []string) ([]Package, error) {
	args := []string{"list", "-e", "-json", "-deps", "-export"}
	if tags != "" {
		args = append(args, "-tags="+tags)
	}
	cmd := exec.CommandContext(ctx, "go", append(args, patterns...)...)
	cmd.Dir = dir
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("go list: %w: %s", err, stderr.String())
	}
	var pkgs []Package
	dec := json.NewDecoder(bytes.NewReader(out))
	for {
		var p Package
		if err := dec.Decode(&p); errors.Is(err, io.EOF) {
			break
		} else if err != nil {
			return nil, fmt.Errorf("decode go list output: %w", err)
		}
		pkgs = append(pkgs, p)
	}
	return pkgs, nil
}

// componentIndex maps package paths to component IDs.
type componentIndex struct {
	module string
	comps  map[string]Component
	cache  map[string]string
}

func newComponentIndex(module string, comps map[string]Component) *componentIndex {
	return &componentIndex{module: module, comps: comps, cache: map[string]string{}}
}

// rel returns a module package path relative to the module root, and false
// for packages outside the module.
func (ix *componentIndex) rel(importPath string) (string, bool) {
	if importPath == ix.module {
		return ".", true
	}
	r, ok := strings.CutPrefix(importPath, ix.module+"/")
	return r, ok
}

// of returns the component holding a package: internal components match the
// module-relative path, external ones the full import path. The most specific
// match wins. It returns "" for packages no component covers.
func (ix *componentIndex) of(importPath string) string {
	if id, ok := ix.cache[importPath]; ok {
		return id
	}
	path, internal := ix.rel(importPath)
	if !internal {
		path = importPath
	}
	best, bestLen := "", -1
	for id, c := range ix.comps {
		if (c.Kind == KindExternal) == internal {
			continue
		}
		for _, p := range c.Match {
			if MatchPattern(p, path) && len(p) > bestLen {
				best, bestLen = id, len(p)
			}
		}
	}
	ix.cache[importPath] = best
	return best
}
