export const DEFAULT_BRAND_NAME = "Zulip";

type ParamsWithBrandingName = {
    branding?: {
        name?: string;
    };
};

export function getBrandName(params: unknown): string {
    return (params as ParamsWithBrandingName).branding?.name ?? DEFAULT_BRAND_NAME;
}
