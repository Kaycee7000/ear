/* Small sample program for testing the EAR pipeline end to end (build: cc -O1 -o notes notes.c).
 * A note keeper: reads its folder from NOTES_DIR or a config file, appends notes, lists and searches them,
 * and keeps an audit log. Written for this repository; it has no other purpose. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define MAX_LINE 512

static char notes_path[400];
static char log_path[400];

static void audit(const char *action, const char *detail) {
    FILE *log = fopen(log_path, "a");
    if (!log) return;
    fprintf(log, "%ld %s %s\n", (long)time(NULL), action, detail);
    fclose(log);
}

static int load_config(void) {
    const char *dir = getenv("NOTES_DIR");
    char line[MAX_LINE];
    if (!dir) {
        FILE *cfg = fopen("/etc/notes.conf", "r");
        if (cfg && fgets(line, sizeof line, cfg) && strncmp(line, "dir=", 4) == 0) {
            line[strcspn(line, "\n")] = 0;
            dir = line + 4;
        }
        if (cfg) fclose(cfg);
    }
    if (!dir) dir = "/var/lib/notes";
    snprintf(notes_path, sizeof notes_path, "%s/notes.txt", dir);
    snprintf(log_path, sizeof log_path, "%s/audit.log", dir);
    return 0;
}

static int add_note(const char *text) {
    FILE *f = fopen(notes_path, "a");
    if (!f) { perror("notes: cannot open store"); return 1; }
    fprintf(f, "%ld\t%s\n", (long)time(NULL), text);
    fclose(f);
    audit("add", text);
    return 0;
}

static int list_notes(const char *needle) {
    char line[MAX_LINE];
    int shown = 0;
    FILE *f = fopen(notes_path, "r");
    if (!f) { fprintf(stderr, "notes: no notes yet\n"); return 1; }
    while (fgets(line, sizeof line, f)) {
        if (!needle || strstr(line, needle)) { fputs(line, stdout); shown++; }
    }
    fclose(f);
    audit(needle ? "search" : "list", needle ? needle : "-");
    printf("%d note(s)\n", shown);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: notes add <text> | list | search <word>\n");
        return 2;
    }
    load_config();
    if (strcmp(argv[1], "add") == 0 && argc > 2) return add_note(argv[2]);
    if (strcmp(argv[1], "list") == 0) return list_notes(NULL);
    if (strcmp(argv[1], "search") == 0 && argc > 2) return list_notes(argv[2]);
    fprintf(stderr, "notes: unknown command %s\n", argv[1]);
    return 2;
}
