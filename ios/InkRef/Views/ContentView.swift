import SwiftUI
import UniformTypeIdentifiers

/// Open a notebook, clean it up, send it back. That is the whole app, and this file is
/// arranged so nothing else competes with it: the document fills the screen from the moment
/// it opens, one button acts on it, one button sends it on. Everything the engine knows —
/// coverage, collisions, timings, recognition boxes — lives behind a single Developer
/// switch, because it is worth showing on request and worth hiding by default.
@MainActor
struct ContentView: View {
    @Bindable var vm: RefactorViewModel
    @State private var importing = false
    @State private var sharing = false
    @State private var showingOptions = false

    /// GoodNotes' UTI is imported by the app (see Info.plist), but the picker must still
    /// open a file that arrived with a generic type — an unpickable document is a dead demo.
    private static let importable: [UTType] =
        [UTType("com.goodnotes.document"), UTType.zip, UTType.data].compactMap { $0 }

    var body: some View {
        NavigationStack {
            Group {
                if vm.pages.isEmpty { welcome } else { document }
            }
            .navigationTitle(vm.documentName ?? "")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { toolbar }
        }
        .fileImporter(isPresented: $importing, allowedContentTypes: Self.importable) { result in
            switch result {
            case let .success(url): Task { await vm.load(url) }
            case .failure: vm.status = .failed(Self.pickerFailure)
            }
        }
        .sheet(isPresented: $sharing) {
            if let url = vm.exportURL { ShareSheet(items: [url]) }
        }
        .sheet(isPresented: $showingOptions) { optionsSheet }
        .alert("InkRef couldn't do that", isPresented: failed) {
            Button("OK") { vm.status = .idle }
        } message: {
            Text(failureMessage)
        }
        .task { await autoDemo() }
    }

    @ToolbarContentBuilder private var toolbar: some ToolbarContent {
        if !vm.pages.isEmpty {
            ToolbarItem(placement: .topBarLeading) {
                Button("Close", systemImage: "xmark") { withAnimation { vm.reset() } }
                    .labelStyle(.iconOnly)
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button("Options", systemImage: "ellipsis.circle") { showingOptions = true }
                    .labelStyle(.iconOnly)
            }
        }
    }

    // MARK: - nothing open yet

    private var welcome: some View {
        VStack(spacing: 0) {
            Spacer()
            VStack(spacing: 14) {
                Image(systemName: "wand.and.sparkles")
                    .font(.system(size: 52, weight: .light))
                    .foregroundStyle(.tint)
                Text("InkRef")
                    .font(.system(size: 44, weight: .semibold, design: .serif))
                Text("Tidier notes, in your own handwriting.")
                    .font(.title3)
                    .foregroundStyle(.secondary)
            }
            Spacer().frame(height: 44)

            VStack(spacing: 14) {
                Button { importing = true } label: {
                    Label("Open a notebook", systemImage: "folder")
                        .font(.title3.weight(.semibold))
                        .frame(maxWidth: 420, minHeight: 56)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)

                Text("or share one to InkRef from GoodNotes")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)

                Button("Try a sample") { Task { await vm.loadSample() } }
                    .padding(.top, 6)
            }

            if vm.status == .loading {
                stage("Opening notes").padding(.top, 36)
            }
            Spacer()
            Spacer()
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 32)
    }

    // MARK: - a document is open

    private var document: some View {
        VStack(spacing: 0) {
            ScrollView {
                LazyVStack(spacing: 28) {
                    ForEach(vm.pages) { page in pageCard(page) }
                }
                .frame(maxWidth: .infinity)
                .padding(.horizontal, 20)
                .padding(.top, 16)
                .padding(.bottom, 28)
            }
            Divider()
            actionBar
        }
    }

    private func pageCard(_ page: PagePreview) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            PreviewCanvas(strokes: page.strokes, offsets: page.offsets,
                          analysis: vm.hasPlan ? page.analysis : nil, roles: page.roles,
                          showStructure: vm.showDeveloper && vm.showStructure,
                          recognized: page.recognized, groups: page.groups,
                          unmatched: page.unmatched,
                          showRecognition: vm.showDeveloper && vm.showRecognition,
                          progress: vm.showRefactored ? 1 : 0,
                          paperSize: page.paperSize, background: page.background)
                .padding(8)
                // A document preview is a sheet of paper in either colour scheme; dark-mode
                // black ink on a dark ground would be invisible.
                .background(Color.white, in: RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12)
                    .strokeBorder(.black.opacity(0.08), lineWidth: 1))
                .shadow(color: .black.opacity(0.18), radius: 14, y: 6)
                .animation(.easeInOut(duration: 0.25), value: vm.showStructure)

            if vm.showDeveloper { developerDetail(page) }
        }
        .frame(maxWidth: 980)
    }

    // MARK: - the one place anything happens

    @ViewBuilder private var actionBar: some View {
        VStack(spacing: 12) {
            if vm.status == .analyzing {
                working
            } else if vm.hasPlan {
                comparison
            } else {
                readyToBeautify
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
        .background(.bar)
        .animation(.default, value: vm.hasPlan)
    }

    private var readyToBeautify: some View {
        VStack(spacing: 10) {
            Text(vm.pageCount == 1 ? "1 page ready" : "\(vm.pageCount) pages ready")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Button { Task { await vm.refactor() } } label: {
                Label("Beautify", systemImage: "wand.and.stars")
                    .font(.title3.weight(.semibold))
                    .frame(maxWidth: 420, minHeight: 54)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
        }
    }

    /// Never a fabricated percentage. The bar tracks pages, which the pipeline genuinely
    /// knows; the words underneath name the stage, which it also genuinely knows. Guessing
    /// a number for the part inside a page would be inventing precision.
    private var working: some View {
        VStack(spacing: 10) {
            stage(vm.progress ?? "Cleaning up notes")
            if let f = vm.progressFraction, vm.pageCount > 1 {
                ProgressView(value: f).frame(maxWidth: 420)
                Text("Page \(min(Int(f * Double(vm.pageCount)) + 1, vm.pageCount)) "
                     + "of \(vm.pageCount)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var comparison: some View {
        VStack(spacing: 10) {
            // In the bar rather than under the page: a full page fills the screen, so a
            // caption beneath it is below the fold exactly when it is worth reading.
            if let first = vm.pages.first {
                Text(first.summary)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 20) { beforeAfterPicker; Spacer(minLength: 8); exportButton }
                VStack(spacing: 12) { beforeAfterPicker; exportButton }
            }
        }
    }

    private var beforeAfterPicker: some View {
        Picker("", selection: beforeAfter) {
            Text("Before").tag(false)
            Text("After").tag(true)
        }
        .pickerStyle(.segmented)
        .frame(width: 260)
    }

    @ViewBuilder private var exportButton: some View {
        if vm.exportURL != nil {
            HStack(spacing: 12) {
                Label("Ready to send", systemImage: "checkmark.circle.fill")
                    .font(.subheadline)
                    .foregroundStyle(.green)
                Button { sharing = true } label: {
                    Label("Open in GoodNotes", systemImage: "square.and.arrow.up")
                        .font(.headline)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
            }
        } else {
            Button {
                Task {
                    await vm.export()
                    sharing = vm.exportURL != nil
                }
            } label: {
                Label(vm.isExporting ? "Preparing export…" : "Open in GoodNotes",
                      systemImage: "square.and.arrow.up")
                    .font(.headline)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(vm.isExporting)
        }
    }

    private func stage(_ text: String) -> some View {
        HStack(spacing: 10) {
            ProgressView().controlSize(.small)
            Text(text + "…").font(.callout).foregroundStyle(.secondary)
        }
        .contentTransition(.opacity)
        .animation(.default, value: text)
    }

    // MARK: - options, and the engineering behind them

    private var optionsSheet: some View {
        NavigationStack {
            Form {
                Section("Formatting") {
                    Picker("Strength", selection: $vm.strength) {
                        ForEach(InkLayout.Strength.all) { s in
                            Text(s.name.capitalized).tag(s)
                        }
                    }
                    .pickerStyle(.segmented)
                }
                Section {
                    Toggle("Developer", isOn: $vm.showDeveloper)
                } footer: {
                    Text("Shows what the engine found: recognised text, stroke groups, "
                         + "protected regions and stage timings.")
                }
                if vm.showDeveloper { developerOptions }
            }
            .navigationTitle("Options")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { showingOptions = false }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }

    @ViewBuilder private var developerOptions: some View {
        Section("Overlays") {
            Toggle("Layout analysis", isOn: $vm.showStructure)
            Toggle("Reading", isOn: $vm.showRecognition)
        }
        Section {
            Toggle("Read the page", isOn: $vm.readPage)
            Picker("Semantics", selection: $vm.aiMode) {
                ForEach(AIMode.allCases, id: \.self) { Text($0.rawValue.capitalized).tag($0) }
            }
        } footer: {
            Text("Recognition runs on device and nothing leaves the iPad. Semantic "
                 + "labelling through Backboard is optional and is never on the path to a "
                 + "result: if it is slow or unavailable, formatting carries on without it.")
        }
        if !vm.timings.isEmpty {
            Section("Timings") {
                Text(vm.timings.summary).font(.caption.monospaced())
            }
        }
    }

    private func developerDetail(_ page: PagePreview) -> some View {
        Text(page.caption)
            .font(.caption.monospaced())
            .foregroundStyle(.tertiary)
    }

    // MARK: - bindings and wording

    private var beforeAfter: Binding<Bool> {
        Binding(get: { vm.showRefactored },
                set: { on in withAnimation(.beautify) { vm.showRefactored = on } })
    }

    private var failed: Binding<Bool> {
        Binding(get: { if case .failed = vm.status { return true }; return false },
                set: { if !$0 { vm.status = .idle } })
    }

    private var failureMessage: String {
        if case let .failed(message) = vm.status { return message }
        return ""
    }

    /// A picker failure is the user's business only in so far as it says what to do next.
    private static let pickerFailure =
        "That document couldn't be opened. Try sharing it to InkRef from GoodNotes instead."

    private func autoDemo() async {
        #if DEBUG
        // Lets each state be captured from the command line; synthetic taps on the
        // simulator are not reliable enough to drive the UI. Debug builds only.
        let args = ProcessInfo.processInfo.arguments
        guard args.contains("-autoDemo") else { return }
        await vm.loadSample()
        if args.contains("-import") { return }        // stop at the freshly opened document
        await vm.refactor()
        if args.contains("-before") { vm.showRefactored = false }
        if args.contains("-developer") { vm.showDeveloper = true }
        if args.contains("-structure") { vm.showDeveloper = true; vm.showStructure = true }
        if args.contains("-autoExport") {
            await vm.export()
            print("EXPORTED \(vm.exportURL?.path ?? "nil")")
        }
        #endif
    }
}

#Preview {
    ContentView(vm: RefactorViewModel())
}
