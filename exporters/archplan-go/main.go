// Command archplan-go writes the architecture of a Go module as an
// archmodel/1 JSON file: components, observed dependencies, the dependency
// policy, types, optional sequences from seqtrace, and the vocabulary.
//
//	archplan-go [-C dir] [-o file] [-archlint file] [-steps file]... [-exclude dirs] [-depth n] [-no-vocab] [packages]
//
// See README.md.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

const (
	filePerm = 0o600
	dirPerm  = 0o750

	// defaultArchLint is read when it exists and -archlint is not given.
	defaultArchLint = ".go-arch-lint.yml"
	// noArchLint as -archlint ignores any go-arch-lint config.
	noArchLint = "none"
)

type listFlag []string

func (l *listFlag) String() string { return strings.Join(*l, ",") }

func (l *listFlag) Set(v string) error {
	for s := range strings.SplitSeq(v, ",") {
		if s = strings.TrimSpace(s); s != "" {
			*l = append(*l, s)
		}
	}
	return nil
}

func main() {
	var steps, exclude listFlag
	dir := flag.String("C", ".", "module root")
	out := flag.String("o", "", "output file (default stdout)")
	archLint := flag.String("archlint", "", "go-arch-lint config, relative to -C (default "+defaultArchLint+" if it exists; \""+noArchLint+"\" to ignore it)")
	tags := flag.String("tags", "", "build tags for go list")
	depth := flag.Int("depth", 0, "group packages without a go-arch-lint component by this many leading path segments (0: one component per package)")
	unexported := flag.Bool("unexported", false, "also export unexported types and methods")
	noVocab := flag.Bool("no-vocab", false, "skip the vocabulary section")
	flag.Var(&steps, "steps", "seqtrace steps.json to add as sequences (repeatable)")
	flag.Var(&exclude, "exclude", "module-relative directories to skip (comma-separated, repeatable)")
	flag.Usage = func() {
		fmt.Fprintln(flag.CommandLine.Output(), "usage: archplan-go [flags] [packages]\n\nPackages default to ./... in the module root.\n\nFlags:")
		flag.PrintDefaults()
	}
	flag.Parse()
	patterns := flag.Args()
	if len(patterns) == 0 {
		patterns = []string{"./..."}
	}
	root, err := filepath.Abs(*dir)
	if err != nil {
		fail(err)
	}
	config, err := archLintConfig(root, *archLint)
	if err != nil {
		fail(err)
	}
	for i, e := range exclude {
		exclude[i] = strings.TrimSuffix(filepath.ToSlash(filepath.Clean(e)), "/")
	}
	m, err := Export(context.Background(), Options{
		Root: root, ArchLint: config, Tags: *tags, Patterns: patterns, Steps: steps, Exclude: exclude,
		Depth: *depth, Unexported: *unexported, NoVocab: *noVocab, Warn: warn,
	})
	if err != nil {
		fail(err)
	}
	if err := write(*out, m); err != nil {
		fail(err)
	}
	var concepts, usage int
	if m.Vocabulary != nil {
		concepts, usage = len(m.Vocabulary.Concepts), len(m.Vocabulary.Usage)
	}
	warn("%d components, %d dependencies, %d types, %d sequences, %d concepts, %d usage rows",
		len(m.Components), len(m.Dependencies), len(m.Types), len(m.Sequences), concepts, usage)
}

// archLintConfig resolves -archlint: the default file when it exists, no
// config for "none", and otherwise the given file, which must exist.
func archLintConfig(root, flagValue string) (string, error) {
	switch flagValue {
	case noArchLint:
		return "", nil
	case "":
		if _, err := os.Stat(filepath.Join(root, defaultArchLint)); errors.Is(err, fs.ErrNotExist) {
			return "", nil
		}
		return defaultArchLint, nil
	}
	return flagValue, nil
}

func write(out string, m *Model) error {
	data, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return fmt.Errorf("encode model: %w", err)
	}
	data = append(data, '\n')
	if out == "" {
		_, err = os.Stdout.Write(data)
		return err
	}
	if err := os.MkdirAll(filepath.Dir(out), dirPerm); err != nil {
		return err
	}
	return os.WriteFile(out, data, filePerm)
}

func warn(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "archplan-go: "+format+"\n", args...)
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "archplan-go:", err)
	os.Exit(1)
}
