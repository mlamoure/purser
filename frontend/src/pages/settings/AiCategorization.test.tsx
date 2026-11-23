import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { apiMock, mockApi, ok } from "../../test/api";
import { configOut } from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import { AiCard, CLOUD_DEFAULT_BASE_URL } from "./AiCategorization";

const freshInstall = () => configOut({ ai_base_url: CLOUD_DEFAULT_BASE_URL });

const passed = {
  ok: true,
  reachable: true,
  authenticated: true,
  model_listed: true,
  answered: true,
  network: "private_network",
  key_sent: false,
  elapsed_ms: 840,
  message: "Connected: llama3.1:8b answered in 0.8 s.",
  warning: null,
};

describe("AI categorization card (#640)", () => {
  it("lets a fresh install set up a keyless local model without a cloud URL to overwrite", async () => {
    const config = freshInstall();
    mockApi({ "PUT /config": ok(config) });
    const { user } = renderWithProviders(<AiCard initial={config} />);

    await user.selectOptions(screen.getByLabelText("Provider"), "local");
    const url = screen.getByLabelText("Base URL");
    expect(url).toHaveValue("");
    expect(url).toHaveAttribute("placeholder", "http://localhost:11434/v1");
    expect(url).toBeRequired();
    expect(screen.getByLabelText("API key (optional)")).toHaveAttribute(
      "placeholder",
      "leave empty if your server needs none",
    );

    await user.type(url, "http://host.docker.internal:11434/v1");
    await user.type(screen.getByLabelText("Model"), "llama3.1:8b");
    await user.clear(screen.getByLabelText("Timeout (seconds)"));
    await user.type(screen.getByLabelText("Timeout (seconds)"), "120");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(apiMock.PUT).toHaveBeenCalledWith("/config", {
        body: {
          ai_provider: "local",
          ai_base_url: "http://host.docker.internal:11434/v1",
          ai_model: "llama3.1:8b",
          ai_timeout_seconds: 120,
        },
      }),
    );
  });

  it("refuses to save a local model without its server's address", async () => {
    const { user } = renderWithProviders(<AiCard initial={freshInstall()} />);

    await user.selectOptions(screen.getByLabelText("Provider"), "local");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(screen.getByRole("alert")).toHaveTextContent("A local model needs its server's base URL");
    expect(apiMock.PUT).not.toHaveBeenCalled();
  });

  it("refuses a timeout outside 5 to 600 seconds", async () => {
    const { user } = renderWithProviders(
      <AiCard initial={configOut({ ai_provider: "local", ai_base_url: "http://10.0.0.5:8080/v1" })} />,
    );

    await user.clear(screen.getByLabelText("Timeout (seconds)"));
    await user.type(screen.getByLabelText("Timeout (seconds)"), "2");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(screen.getByRole("alert")).toHaveTextContent("5 to 600");
    expect(apiMock.PUT).not.toHaveBeenCalled();
  });

  it("states what is sent, and that a local model keeps it at home", async () => {
    const { user } = renderWithProviders(<AiCard initial={freshInstall()} />);
    expect(screen.queryByRole("region", { name: "What Purser sends" })).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Provider"), "local");
    const box = screen.getByRole("region", { name: "What Purser sends" });
    expect(box).toHaveTextContent(
      "To categorize a transaction that no rule or earlier choice covers, Purser sends the server at this address:",
    );
    expect(within(box).getByText("its payee, description and amount")).toBeInTheDocument();
    expect(within(box).getByText("your reviewer checklist")).toBeInTheDocument();
    expect(box).toHaveTextContent("this stays on your network");

    await user.selectOptions(screen.getByLabelText("Provider"), "openai");
    expect(screen.getByRole("region", { name: "What Purser sends" })).toHaveTextContent(
      "With a hosted service, this leaves your network",
    );
    await user.selectOptions(screen.getByLabelText("Provider"), "anthropic");
    expect(screen.getByRole("region", { name: "What Purser sends" })).toHaveTextContent(
      "Purser sends Anthropic:",
    );
  });

  it("gives a hosted service its default URL back and hides the URL for Anthropic", async () => {
    const { user } = renderWithProviders(<AiCard initial={freshInstall()} />);

    await user.selectOptions(screen.getByLabelText("Provider"), "openai");
    expect(screen.getByLabelText("Base URL")).toHaveValue(CLOUD_DEFAULT_BASE_URL);
    expect(screen.getByLabelText("API key")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Provider"), "anthropic");
    expect(screen.queryByLabelText("Base URL")).not.toBeInTheDocument();
  });

  it("keeps an existing OpenAI-compatible setup exactly as configured", async () => {
    const config = configOut({
      ai_provider: "openai",
      ai_base_url: "http://192.168.1.20:4000/v1",
      ai_model: "my-model",
    });
    mockApi({ "PUT /config": ok(config) });
    const { user } = renderWithProviders(<AiCard initial={config} />);

    expect(screen.getByLabelText("Provider")).toHaveValue("openai");
    expect(screen.getByRole("option", { name: "Hosted OpenAI-compatible service" })).toBeInTheDocument();
    expect(screen.getByLabelText("Base URL")).toHaveValue("http://192.168.1.20:4000/v1");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(apiMock.PUT).toHaveBeenCalledWith("/config", {
        body: {
          ai_provider: "openai",
          ai_base_url: "http://192.168.1.20:4000/v1",
          ai_model: "my-model",
          ai_timeout_seconds: 30,
        },
      }),
    );
  });

  it("lists the server's models for the Model field", async () => {
    mockApi({
      "POST /config/ai/models": ok({
        listed: true,
        models: ["llama3.1:8b", "qwen2.5:7b"],
        key_sent: false,
        message: "Found 2 models.",
      }),
    });
    const { user, container } = renderWithProviders(
      <AiCard initial={configOut({ ai_provider: "local", ai_base_url: "http://10.0.0.5:11434/v1" })} />,
    );

    await user.click(screen.getByRole("button", { name: "Find models" }));

    expect(await screen.findByText(/Found 2 models\./)).toBeInTheDocument();
    const listId = screen.getByLabelText("Model").getAttribute("list")!;
    const options = container.querySelectorAll(`datalist[id="${listId}"] option`);
    expect(Array.from(options, (o) => o.getAttribute("value"))).toEqual(["llama3.1:8b", "qwen2.5:7b"]);
    expect(apiMock.POST).toHaveBeenCalledWith("/config/ai/models", {
      body: {
        provider: "local",
        use_stored_key: true,
        base_url: "http://10.0.0.5:11434/v1",
        timeout_seconds: 30,
      },
    });
  });

  it("says when the server has no model list", async () => {
    mockApi({
      "POST /config/ai/models": ok({
        listed: false,
        models: [],
        key_sent: false,
        message: "This server does not list its models: type the model name.",
      }),
    });
    const { user } = renderWithProviders(
      <AiCard initial={configOut({ ai_provider: "local", ai_base_url: "http://10.0.0.5:8080/v1" })} />,
    );

    await user.click(screen.getByRole("button", { name: "Find models" }));

    expect(
      await screen.findByText("This server does not list its models: type the model name."),
    ).toBeInTheDocument();
  });

  it("tests the connection with what is in the form, before saving", async () => {
    mockApi({ "POST /config/ai/test": ok(passed) });
    const { user } = renderWithProviders(<AiCard initial={freshInstall()} />);

    await user.selectOptions(screen.getByLabelText("Provider"), "local");
    await user.type(screen.getByLabelText("Base URL"), "http://192.168.1.20:11434/v1");
    await user.type(screen.getByLabelText("Model"), "llama3.1:8b");
    await user.click(screen.getByRole("button", { name: "Test connection" }));

    expect(await screen.findByText("Connected: llama3.1:8b answered in 0.8 s.")).toBeInTheDocument();
    expect(apiMock.POST).toHaveBeenCalledWith("/config/ai/test", {
      body: {
        provider: "local",
        use_stored_key: false, // not the saved provider and address (#667)
        base_url: "http://192.168.1.20:11434/v1",
        model: "llama3.1:8b",
        timeout_seconds: 30,
      },
    });
    expect(apiMock.PUT).not.toHaveBeenCalled();
  });

  it("shows a failed test and a public-address warning", async () => {
    mockApi({
      "POST /config/ai/test": ok({
        ...passed,
        ok: false,
        authenticated: false,
        answered: null,
        network: "public_internet",
        message: "The server refused the API key (HTTP 401). Check the key.",
        warning: "This server's address is on the public internet, so what Purser sends leaves your network.",
      }),
    });
    const { user } = renderWithProviders(
      <AiCard
        initial={configOut({ ai_provider: "local", ai_base_url: "http://models.example.com/v1", ai_model: "m" })}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Test connection" }));

    expect(
      await screen.findByText("The server refused the API key (HTTP 401). Check the key."),
    ).toBeInTheDocument();
    expect(screen.getByText(/on the public internet/)).toBeInTheDocument();
  });

  it("clears a stored key with an explicit no-key setting", async () => {
    const config = configOut(
      { ai_provider: "local", ai_base_url: "http://10.0.0.5:11434/v1" },
      { ai_api_key_set: true },
    );
    mockApi({ "PUT /config": ok(config) });
    const { user } = renderWithProviders(<AiCard initial={config} />);

    expect(screen.getByText(/A key is stored, and Purser sends it to this server/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear key" }));

    await waitFor(() =>
      expect(apiMock.PUT).toHaveBeenCalledWith("/config", { body: { ai_api_key: null } }),
    );
  });

  it("offers the stored key only while the provider and address are the saved ones (#667)", async () => {
    mockApi({
      "POST /config/ai/models": ok({ listed: true, models: ["m"], key_sent: false, message: "Found 1 models." }),
    });
    const config = configOut(
      { ai_provider: "openai", ai_base_url: CLOUD_DEFAULT_BASE_URL, ai_model: "m" },
      { ai_api_key_set: true },
    );
    const { user } = renderWithProviders(<AiCard initial={config} />);
    const lastBody = () => apiMock.POST.mock.calls.at(-1)?.[1]?.body;
    const hint = /send the stored key only to the saved provider and address/;

    // The saved destination (a trailing slash is the same address).
    await user.type(screen.getByLabelText("Base URL"), "/");
    expect(screen.queryByText(hint)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Find models" }));
    await waitFor(() => expect(lastBody()).toMatchObject({ provider: "openai", use_stored_key: true }));

    // Another address: the check goes without the stored key.
    await user.clear(screen.getByLabelText("Base URL"));
    await user.type(screen.getByLabelText("Base URL"), "http://192.168.1.20:11434/v1");
    expect(screen.getByText(hint)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Find models" }));
    await waitFor(() => expect(lastBody()).toMatchObject({ use_stored_key: false }));

    // Another provider at that address: still not the saved destination.
    await user.selectOptions(screen.getByLabelText("Provider"), "local");
    expect(screen.getByText(/A key is stored, and saving sends it to this server/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Find models" }));
    await waitFor(() =>
      expect(lastBody()).toMatchObject({ provider: "local", use_stored_key: false }),
    );

    // A typed key is the request's own: it goes, and the hint goes away.
    await user.type(screen.getByLabelText("API key (optional)"), "typed-key");
    expect(screen.queryByText(hint)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Find models" }));
    await waitFor(() => expect(lastBody()).toMatchObject({ api_key: "typed-key" }));
  });
});
