from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.access.models import Permission, Role, RolePermission, UserRoleAssignment
from apps.budgets.models import Budget, BudgetCategory, BudgetLine, BudgetSubCategory
from apps.core.models import Organization, ScopeNode
from apps.users.models import User
from apps.vendors.models import VendorSubmissionRoute
from apps.workflow.models import (
    StepGroup,
    VersionStatus,
    WorkflowSplitOption,
    WorkflowStep,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)


PASSWORD = "Hiparks@123"


ROLE_SPECS = {
    "marketing_executive": {
        "name": "Marketing Executive",
        "permissions": [
            ("read", "budget"),
            ("read", "campaign"),
            ("approve", "invoice"),
            ("create", "invoice"),
            ("read", "invoice"),
            ("reject", "invoice"),
            ("start_workflow", "invoice"),
            ("update", "invoice"),
            ("read", "vendor"),
            ("approve", "workflow"),
            ("read", "workflow"),
            ("reject", "workflow"),
        ],
    },
    "marketing_manager": {
        "name": "MarketingManager",
        "permissions": [
            ("read", "budget"),
            ("read", "campaign"),
            ("approve", "invoice"),
            ("create", "invoice"),
            ("read", "invoice"),
            ("reject", "invoice"),
            ("start_workflow", "invoice"),
            ("update", "invoice"),
            ("read", "vendor"),
            ("approve", "workflow"),
            ("read", "workflow"),
            ("reject", "workflow"),
        ],
    },
    "hod": {
        "name": "HOD",
        "permissions": [
            ("read", "budget"),
            ("read", "campaign"),
            ("approve", "invoice"),
            ("create", "invoice"),
            ("read", "invoice"),
            ("reject", "invoice"),
            ("start_workflow", "invoice"),
            ("update", "invoice"),
            ("read", "vendor"),
            ("approve", "workflow"),
            ("read", "workflow"),
            ("reject", "workflow"),
        ],
    },
    "finance_team": {
        "name": "Finance Team",
        "permissions": [
            ("read", "budget"),
            ("read", "campaign"),
            ("approve", "invoice"),
            ("read", "invoice"),
            ("reject", "invoice"),
            ("read", "vendor"),
            ("approve", "workflow"),
            ("read", "workflow"),
            ("reject", "workflow"),
        ],
    },
    "tenant_admin": {
        "name": "Tenant Admin",
        "permissions": [
            ("approve", "budget"), ("create", "budget"), ("delete", "budget"), ("manage_module", "budget"),
            ("read", "budget"), ("reassign", "budget"), ("reject", "budget"), ("start_workflow", "budget"), ("update", "budget"),
            ("approve", "campaign"), ("create", "campaign"), ("delete", "campaign"), ("manage_module", "campaign"),
            ("read", "campaign"), ("reassign", "campaign"), ("reject", "campaign"), ("start_workflow", "campaign"), ("update", "campaign"),
            ("approve", "invoice"), ("create", "invoice"), ("delete", "invoice"), ("manage_module", "invoice"),
            ("read", "invoice"), ("reassign", "invoice"), ("reject", "invoice"), ("start_workflow", "invoice"), ("update", "invoice"),
            ("approve", "module"), ("create", "module"), ("delete", "module"), ("manage_module", "module"),
            ("read", "module"), ("reassign", "module"), ("reject", "module"), ("start_workflow", "module"), ("update", "module"),
            ("approve", "role"), ("create", "role"), ("delete", "role"), ("manage_module", "role"),
            ("read", "role"), ("reassign", "role"), ("reject", "role"), ("start_workflow", "role"), ("update", "role"),
            ("approve", "user"), ("create", "user"), ("delete", "user"), ("manage_module", "user"),
            ("read", "user"), ("reassign", "user"), ("reject", "user"), ("start_workflow", "user"), ("update", "user"),
            ("approve", "vendor"), ("create", "vendor"), ("delete", "vendor"), ("manage_module", "vendor"),
            ("read", "vendor"), ("reassign", "vendor"), ("reject", "vendor"), ("start_workflow", "vendor"), ("update", "vendor"),
            ("approve", "workflow"), ("create", "workflow"), ("delete", "workflow"), ("manage_module", "workflow"),
            ("read", "workflow"), ("reassign", "workflow"), ("reject", "workflow"), ("start_workflow", "workflow"), ("update", "workflow"),
        ],
    },
}


USER_SPECS = [
    ("Sanket.ambekar@hiparks.com", "Sanket", "Ambekar", False, "marketing_executive"),
    ("Bhakti.shah@hiparks.com", "Bhakti", "Shah", False, "marketing_executive"),
    ("Arjun.punjabi@hiparks.com", "Arjun", "Punjabi", False, "marketing_executive"),
    ("kajal.tiwari@hiparks.com", "Kajal", "Tiwari", False, "marketing_executive"),
    ("Ronnie.Zaiwalla@hiparks.com", "Ronnie", "Zaiwalla", False, "marketing_manager"),
    ("Taruna.Mahajan@hiparks.com", "Taruna", "Mahajan", False, "hod"),
    ("prathameshmaratheit@gmail.com", "Prathmesh", "Marathe", False, "finance_team"),
    ("HorizonAdmin@hiparks.com", "Horizon", "Admin", True, "tenant_admin"),
]


CATEGORY_SPECS = [
    ("CUSTOMER-IPC-EVENTS", "Customer/IPC Events"),
    ("RETAINERSHIPS-ANNUAL-PAYMENTS", "Retainerships/ annual payments"),
    ("CONTENT-MARKETING-ASSETS", "Content Marketing & Assets"),
    ("PRINT-OUTDOOR", "Print/Outdoor"),
    ("OTHERS", "Others"),
    ("ESG-INITIATIVES", "ESG Initiatives"),
    ("FMO-PARK-BRANDING", "FMO & Park Branding"),
    ("TENANT-ENGAGEMENT", "Tenant Engagement"),
    ("OUTDOOR-MEDIANS-LOCAL", "outdoor medians (local)"),
    ("ONSITE-HOARDING-PERIMETER-BRANDING", "Onsite Hoarding perimeter branding"),
    ("UPGRADES-PHOTOSHOOT-VIDEO", "Upgrades Photoshoot/ Video"),
    ("CMVS-BLOCK-PANORAMA-TIMELAPSE", "CMVs, Block, Panorama & Timelapse"),
    ("BEFORE-AFTER-SHOOT", "Before After shoot"),
    ("PARK-MARKETING-VIDEOS", "Park Marketing videos"),
    ("CLIENT-VISITS-BRANDING", "Client Visits Branding"),
    ("MISC", "Misc."),
    ("OUTDOOR-LOCAL", "outdoor (local)"),
    ("CMVS-PANORAMA-TIMELAPSE", "CMVs, Panorama & Timelapse"),
    ("BEFORE-AFTER", "Before/after"),
    ("CLIENT-VISITS", "Client Visits"),
    ("GROUND-BREAKING-EVENT", "Ground Breaking Event"),
    ("BROCHURE-PRINTING", "Brochure Printing"),
]


SUBCATEGORY_SPECS = [
    ("CUSTOMER-IPC-EVENTS", "PARTNERS-MEET", "Partners Meet"),
    ("CUSTOMER-IPC-EVENTS", "BD-INDUSTRY-FORUM-MEMBERSHIP", "BD - Industry forum membership"),
    ("CUSTOMER-IPC-EVENTS", "BD-SPONSORED-INDUSTRY-EVENTS-INDUSTRY-VISITS", "BD - Sponsored industry events & Industry Visits"),
    ("RETAINERSHIPS-ANNUAL-PAYMENTS", "BRANDING-AND-CREATIVE-AGENCY", "Branding and creative agency"),
    ("RETAINERSHIPS-ANNUAL-PAYMENTS", "DIGITAL-SOCIAL-AGENCY", "Digital/social agency"),
    ("RETAINERSHIPS-ANNUAL-PAYMENTS", "WEBSITE-AMC", "Website AMC"),
    ("RETAINERSHIPS-ANNUAL-PAYMENTS", "WEBSITE-HOSTING-CHARGES", "Website Hosting Charges"),
    ("RETAINERSHIPS-ANNUAL-PAYMENTS", "MISC-CREATIVE-EXP", "Misc - creative exp"),
    ("RETAINERSHIPS-ANNUAL-PAYMENTS", "PRESS-AND-MEDIA-MANAGEMENT", "Press and Media Management"),
    ("CONTENT-MARKETING-ASSETS", "WEBSITE-DEVELOPMENT", "Website Development"),
    ("CONTENT-MARKETING-ASSETS", "MICROBLOGS-BLOGS-FOR-SEO-AND-SMM", "Microblogs Blogs for SEO and SMM"),
    ("CONTENT-MARKETING-ASSETS", "DIGITAL-MEDIA-BUYING", "Digital Media Buying"),
    ("CONTENT-MARKETING-ASSETS", "BRAND-MANUAL", "Brand Manual"),
    ("CONTENT-MARKETING-ASSETS", "CORPORATE-VIDEO", "Corporate Video"),
    ("CONTENT-MARKETING-ASSETS", "CORPORATE-BROCHURE", "Corporate Brochure"),
    ("CONTENT-MARKETING-ASSETS", "CONTENT-MARKETING-ASSETS", "Content Marketing Assets"),
    ("CONTENT-MARKETING-ASSETS", "TENANT-NEWSLETTER", "Tenant Newsletter"),
    ("CONTENT-MARKETING-ASSETS", "AVAILABILITY-NEWSLETTER-FOR-BROKERS", "Availability Newsletter (for brokers)"),
    ("CONTENT-MARKETING-ASSETS", "PROMOTIONS-FOR-IN-CITY-PORTFOLIO-NEW", "Promotions for in-city portfolio (NEW)"),
    ("CONTENT-MARKETING-ASSETS", "WHITEPAPER-SPECIAL-PUBLICATION", "Whitepaper/ special publication"),
    ("CONTENT-MARKETING-ASSETS", "BD-GIVEAWAYS", "BD giveaways"),
    ("PRINT-OUTDOOR", "BRAND-LEVEL-PRINTS-COLLATERALS", "Brand level prints collaterals"),
    ("PRINT-OUTDOOR", "TRADE-AND-BUSINESS-PRINT-ADVTS-BUSINESS-AS-USUAL", "Trade and Business Print Advts (Business as usual)"),
    ("OTHERS", "AWARD-NOMINATIONS-NEW", "Award Nominations (NEW)"),
    ("OTHERS", "MISCELLANEOUS", "Miscellaneous"),
    ("OTHERS", "L-D-COST-FOR-MARKETING-TEAM", "L&D cost for marketing team."),
    ("OTHERS", "DELEGATE-COST-LEASING-BD-TEAM-FOR-TRADE-EVENTS", "Delegate cost leasing & BD team for trade events."),
    ("ESG-INITIATIVES", "MASTER-CLASS-EVENT-SUSTAINABLE-IRE-PROPOSED-THEME-", "Master Class Event - Sustainable IRE (Proposed theme for post IPO customer meet)"),
    ("ESG-INITIATIVES", "SKILL-CENTER-INAUGURATION-AT-FARUKHNAGAR-II", "Skill Center Inauguration at Farukhnagar II"),
    ("ESG-INITIATIVES", "NATIONAL-SAFETY-WEEK-MARCH-NATIONAL-PUBLIC-HEALTH-", "National Safety Week (March), National Public Health Week (April) - Tenant Engagement"),
    ("ESG-INITIATIVES", "VIDEO-PHOTO-DOCUMENTATION", "Video/Photo documentation"),
]


BUDGET_SPECS = [
    ("FY27-MKT-CORP", "FY27 Marketing - Corporate", "Corporate", "2026-27", "yearly", "INR", "active"),
    ("FY27-MKT-NORTH", "FY27 Marketing - North", "North", "2026-27", "yearly", "INR", "active"),
    ("FY27-MKT-SOUTH", "FY27 Marketing - South", "South", "2026-27", "yearly", "INR", "active"),
    ("FY27-MKT-WEST", "FY27 Marketing - West", "West", "2026-27", "yearly", "INR", "active"),
    ("FY27-MKT-INCITY", "FY27 Marketing - Incity", "Incity", "2026-27", "yearly", "INR", "active"),
]


BUDGET_LINE_SPECS = [
    ("FY27-MKT-CORP", "CUSTOMER-IPC-EVENTS", "PARTNERS-MEET", "7000000.00", "0.00", "1225000.00"),
    ("FY27-MKT-CORP", "CUSTOMER-IPC-EVENTS", "BD-INDUSTRY-FORUM-MEMBERSHIP", "700000.00", "0.00", "696310.00"),
    ("FY27-MKT-CORP", "CUSTOMER-IPC-EVENTS", "BD-SPONSORED-INDUSTRY-EVENTS-INDUSTRY-VISITS", "3870000.00", "0.00", "3859403.00"),
    ("FY27-MKT-CORP", "RETAINERSHIPS-ANNUAL-PAYMENTS", "BRANDING-AND-CREATIVE-AGENCY", "2040000.00", "0.00", "1920400.00"),
    ("FY27-MKT-CORP", "RETAINERSHIPS-ANNUAL-PAYMENTS", "DIGITAL-SOCIAL-AGENCY", "1500000.00", "0.00", "1270000.00"),
    ("FY27-MKT-CORP", "RETAINERSHIPS-ANNUAL-PAYMENTS", "WEBSITE-AMC", "460000.00", "0.00", "337780.00"),
    ("FY27-MKT-CORP", "RETAINERSHIPS-ANNUAL-PAYMENTS", "WEBSITE-HOSTING-CHARGES", "61200.00", "0.00", "111406.00"),
    ("FY27-MKT-CORP", "RETAINERSHIPS-ANNUAL-PAYMENTS", "MISC-CREATIVE-EXP", "360000.00", "0.00", "246500.00"),
    ("FY27-MKT-CORP", "RETAINERSHIPS-ANNUAL-PAYMENTS", "PRESS-AND-MEDIA-MANAGEMENT", "3976600.00", "0.00", "3176000.00"),
    ("FY27-MKT-CORP", "CONTENT-MARKETING-ASSETS", "WEBSITE-DEVELOPMENT", "1500000.00", "0.00", "1438000.00"),
    ("FY27-MKT-CORP", "CONTENT-MARKETING-ASSETS", "MICROBLOGS-BLOGS-FOR-SEO-AND-SMM", "150000.00", "0.00", "40000.00"),
    ("FY27-MKT-CORP", "CONTENT-MARKETING-ASSETS", "DIGITAL-MEDIA-BUYING", "2450000.00", "0.00", "2078750.00"),
    ("FY27-MKT-CORP", "CONTENT-MARKETING-ASSETS", "BRAND-MANUAL", "180000.00", "0.00", "180000.00"),
    ("FY27-MKT-CORP", "CONTENT-MARKETING-ASSETS", "CORPORATE-VIDEO", "900000.00", "0.00", "0.00"),
    ("FY27-MKT-CORP", "CONTENT-MARKETING-ASSETS", "CORPORATE-BROCHURE", "800000.00", "0.00", "0.00"),
    ("FY27-MKT-CORP", "CONTENT-MARKETING-ASSETS", "CONTENT-MARKETING-ASSETS", "5000000.00", "0.00", "3038600.00"),
    ("FY27-MKT-CORP", "CONTENT-MARKETING-ASSETS", "TENANT-NEWSLETTER", "200000.00", "0.00", "133000.00"),
    ("FY27-MKT-CORP", "CONTENT-MARKETING-ASSETS", "AVAILABILITY-NEWSLETTER-FOR-BROKERS", "200000.00", "0.00", "99381.00"),
    ("FY27-MKT-CORP", "CONTENT-MARKETING-ASSETS", "PROMOTIONS-FOR-IN-CITY-PORTFOLIO-NEW", "1200000.00", "0.00", "780000.00"),
    ("FY27-MKT-CORP", "CONTENT-MARKETING-ASSETS", "WHITEPAPER-SPECIAL-PUBLICATION", "1350000.00", "0.00", "500000.00"),
    ("FY27-MKT-CORP", "CONTENT-MARKETING-ASSETS", "BD-GIVEAWAYS", "0.00", "0.00", "120000.00"),
    ("FY27-MKT-CORP", "PRINT-OUTDOOR", "BRAND-LEVEL-PRINTS-COLLATERALS", "600000.00", "0.00", "382600.00"),
    ("FY27-MKT-CORP", "PRINT-OUTDOOR", "TRADE-AND-BUSINESS-PRINT-ADVTS-BUSINESS-AS-USUAL", "850000.00", "0.00", "876000.00"),
    ("FY27-MKT-CORP", "OTHERS", "AWARD-NOMINATIONS-NEW", "250000.00", "0.00", "249187.00"),
    ("FY27-MKT-CORP", "OTHERS", "MISCELLANEOUS", "1000000.00", "0.00", "465363.00"),
    ("FY27-MKT-CORP", "OTHERS", "L-D-COST-FOR-MARKETING-TEAM", "245000.00", "0.00", "0.00"),
    ("FY27-MKT-CORP", "OTHERS", "DELEGATE-COST-LEASING-BD-TEAM-FOR-TRADE-EVENTS", "900000.00", "0.00", "126062.00"),
    ("FY27-MKT-CORP", "ESG-INITIATIVES", "MASTER-CLASS-EVENT-SUSTAINABLE-IRE-PROPOSED-THEME-", "2000000.00", "0.00", "0.00"),
    ("FY27-MKT-CORP", "ESG-INITIATIVES", "SKILL-CENTER-INAUGURATION-AT-FARUKHNAGAR-II", "1000000.00", "0.00", "0.00"),
    ("FY27-MKT-CORP", "ESG-INITIATIVES", "NATIONAL-SAFETY-WEEK-MARCH-NATIONAL-PUBLIC-HEALTH-", "2600000.00", "0.00", "2074850.00"),
    ("FY27-MKT-CORP", "ESG-INITIATIVES", "VIDEO-PHOTO-DOCUMENTATION", "2000000.00", "0.00", "878000.00"),
    ("FY27-MKT-NORTH", "FMO-PARK-BRANDING", None, "8670000.00", "0.00", "0.00"),
    ("FY27-MKT-NORTH", "TENANT-ENGAGEMENT", None, "4400000.00", "0.00", "0.00"),
    ("FY27-MKT-NORTH", "OUTDOOR-MEDIANS-LOCAL", None, "2000000.00", "0.00", "0.00"),
    ("FY27-MKT-NORTH", "ONSITE-HOARDING-PERIMETER-BRANDING", None, "590000.00", "0.00", "0.00"),
    ("FY27-MKT-NORTH", "UPGRADES-PHOTOSHOOT-VIDEO", None, "800000.00", "0.00", "6875.00"),
    ("FY27-MKT-NORTH", "CMVS-BLOCK-PANORAMA-TIMELAPSE", None, "225000.00", "0.00", "0.00"),
    ("FY27-MKT-NORTH", "BEFORE-AFTER-SHOOT", None, "50000.00", "0.00", "8800.00"),
    ("FY27-MKT-NORTH", "PARK-MARKETING-VIDEOS", None, "50000.00", "0.00", "0.00"),
    ("FY27-MKT-NORTH", "CLIENT-VISITS-BRANDING", None, "200000.00", "0.00", "0.00"),
    ("FY27-MKT-NORTH", "MISC", None, "250000.00", "0.00", "0.00"),
    ("FY27-MKT-SOUTH", "FMO-PARK-BRANDING", None, "19800000.00", "0.00", "0.00"),
    ("FY27-MKT-SOUTH", "TENANT-ENGAGEMENT", None, "6600000.00", "0.00", "0.00"),
    ("FY27-MKT-SOUTH", "OUTDOOR-LOCAL", None, "2000000.00", "0.00", "0.00"),
    ("FY27-MKT-SOUTH", "ONSITE-HOARDING-PERIMETER-BRANDING", None, "2907000.00", "0.00", "0.00"),
    ("FY27-MKT-SOUTH", "UPGRADES-PHOTOSHOOT-VIDEO", None, "800000.00", "0.00", "0.00"),
    ("FY27-MKT-SOUTH", "CMVS-PANORAMA-TIMELAPSE", None, "1062500.00", "0.00", "0.00"),
    ("FY27-MKT-SOUTH", "BEFORE-AFTER", None, "150000.00", "0.00", "0.00"),
    ("FY27-MKT-SOUTH", "PARK-MARKETING-VIDEOS", None, "850000.00", "0.00", "0.00"),
    ("FY27-MKT-SOUTH", "CLIENT-VISITS", None, "450000.00", "0.00", "0.00"),
    ("FY27-MKT-SOUTH", "MISC", None, "700000.00", "0.00", "0.00"),
    ("FY27-MKT-WEST", "FMO-PARK-BRANDING", None, "11850000.00", "0.00", "0.00"),
    ("FY27-MKT-WEST", "TENANT-ENGAGEMENT", None, "2200000.00", "0.00", "0.00"),
    ("FY27-MKT-WEST", "OUTDOOR-LOCAL", None, "4000000.00", "0.00", "0.00"),
    ("FY27-MKT-WEST", "UPGRADES-PHOTOSHOOT-VIDEO", None, "400000.00", "0.00", "0.00"),
    ("FY27-MKT-WEST", "ONSITE-HOARDING-PERIMETER-BRANDING", None, "9500000.00", "0.00", "0.00"),
    ("FY27-MKT-WEST", "CMVS-PANORAMA-TIMELAPSE", None, "700000.00", "0.00", "0.00"),
    ("FY27-MKT-WEST", "PARK-MARKETING-VIDEOS", None, "350000.00", "0.00", "0.00"),
    ("FY27-MKT-WEST", "CLIENT-VISITS", None, "150000.00", "0.00", "0.00"),
    ("FY27-MKT-WEST", "GROUND-BREAKING-EVENT", None, "1500000.00", "0.00", "0.00"),
    ("FY27-MKT-WEST", "MISC", None, "400000.00", "0.00", "0.00"),
    ("FY27-MKT-INCITY", "FMO-PARK-BRANDING", None, "6000000.00", "0.00", "0.00"),
    ("FY27-MKT-INCITY", "BROCHURE-PRINTING", None, "425000.00", "0.00", "0.00"),
    ("FY27-MKT-INCITY", "OUTDOOR-LOCAL", None, "2400000.00", "0.00", "0.00"),
    ("FY27-MKT-INCITY", "ONSITE-HOARDING-PERIMETER-BRANDING", None, "320000.00", "0.00", "0.00"),
    ("FY27-MKT-INCITY", "CMVS-PANORAMA-TIMELAPSE", None, "1890000.00", "0.00", "0.00"),
    ("FY27-MKT-INCITY", "CLIENT-VISITS", None, "850000.00", "0.00", "0.00"),
    ("FY27-MKT-INCITY", "MISC", None, "850000.00", "0.00", "0.00"),
]


WORKFLOW_SPECS = [
    {
        "code": "invoice-send-to-sanket",
        "name": "Invoice - Send To Sanket",
        "route": ("send-to-sanket", "Send To Sanket", 1),
        "version_number": 2,
        "groups": [
            {
                "name": "Marketing Executive Review",
                "display_order": 1,
                "parallel_mode": "SINGLE",
                "on_rejection_action": "TERMINATE",
                "goto_order": None,
                "steps": [
                    {
                        "name": "Marketing Executive Review",
                        "display_order": 1,
                        "required_role": "marketing_executive",
                        "scope_resolution_policy": "SUBJECT_NODE",
                        "default_user": "Sanket.ambekar@hiparks.com",
                        "step_kind": "NORMAL_APPROVAL",
                    }
                ],
            },
            {
                "name": "Runtime Split Allocation",
                "display_order": 2,
                "parallel_mode": "SINGLE",
                "on_rejection_action": "BRANCH_CORRECTION",
                "goto_order": None,
                "steps": [
                    {
                        "name": "Runtime Split Allocation",
                        "display_order": 1,
                        "required_role": "marketing_executive",
                        "scope_resolution_policy": "SUBJECT_NODE",
                        "default_user": "Sanket.ambekar@hiparks.com",
                        "step_kind": "RUNTIME_SPLIT_ALLOCATION",
                        "allocation_total_policy": "MUST_EQUAL_INVOICE_TOTAL",
                        "approver_selection_mode": "RUNTIME_SELECTED_FROM_POOL",
                        "require_category": True,
                        "require_budget": True,
                        "allow_multiple_lines_per_entity": False,
                        "branch_approval_policy": "REQUIRED_FOR_ALL",
                        "split_entities": [
                            ("Corporate", ["Ronnie.Zaiwalla@hiparks.com"]),
                            ("North", ["Ronnie.Zaiwalla@hiparks.com"]),
                            ("South", ["Ronnie.Zaiwalla@hiparks.com"]),
                            ("West", ["Ronnie.Zaiwalla@hiparks.com"]),
                            ("Incity", ["Ronnie.Zaiwalla@hiparks.com"]),
                        ],
                    }
                ],
            },
            {
                "name": "HOD Review",
                "display_order": 3,
                "parallel_mode": "SINGLE",
                "on_rejection_action": "GO_TO_GROUP",
                "goto_order": 2,
                "steps": [
                    {
                        "name": "HOD Review",
                        "display_order": 1,
                        "required_role": "hod",
                        "scope_resolution_policy": "SUBJECT_NODE",
                        "default_user": "Taruna.Mahajan@hiparks.com",
                        "step_kind": "NORMAL_APPROVAL",
                    }
                ],
            },
        ],
    },
    {
        "code": "invoice-send-to-bhakti",
        "name": "Invoice - Send To Bhakti",
        "route": ("send-to-bhakti", "Send To Bhakti", 2),
        "version_number": 2,
        "groups": [
            {
                "name": "Marketing Executive Review",
                "display_order": 1,
                "parallel_mode": "SINGLE",
                "on_rejection_action": "TERMINATE",
                "goto_order": None,
                "steps": [
                    {
                        "name": "Marketing Executive Review",
                        "display_order": 1,
                        "required_role": "marketing_executive",
                        "scope_resolution_policy": "SUBJECT_NODE",
                        "default_user": "Bhakti.shah@hiparks.com",
                        "step_kind": "NORMAL_APPROVAL",
                    }
                ],
            },
            {
                "name": "Runtime Split Allocation",
                "display_order": 2,
                "parallel_mode": "SINGLE",
                "on_rejection_action": "BRANCH_CORRECTION",
                "goto_order": None,
                "steps": [
                    {
                        "name": "Runtime Split Allocation",
                        "display_order": 1,
                        "required_role": "marketing_executive",
                        "scope_resolution_policy": "SUBJECT_NODE",
                        "default_user": "Bhakti.shah@hiparks.com",
                        "step_kind": "RUNTIME_SPLIT_ALLOCATION",
                        "allocation_total_policy": "MUST_EQUAL_INVOICE_TOTAL",
                        "approver_selection_mode": "RUNTIME_SELECTED_FROM_POOL",
                        "require_category": True,
                        "require_budget": True,
                        "allow_multiple_lines_per_entity": False,
                        "branch_approval_policy": "REQUIRED_FOR_ALL",
                        "split_entities": [
                            ("Corporate", ["Taruna.Mahajan@hiparks.com"]),
                            ("North", ["Taruna.Mahajan@hiparks.com"]),
                            ("South", ["Taruna.Mahajan@hiparks.com"]),
                            ("West", ["Taruna.Mahajan@hiparks.com"]),
                            ("Incity", ["Taruna.Mahajan@hiparks.com"]),
                        ],
                    }
                ],
            },
        ],
    },
    {
        "code": "invoice-send-to-arjun",
        "name": "Invoice - Send To Arjun",
        "route": ("send-to-arjun", "Send To Arjun", 3),
        "version_number": 2,
        "groups": [
            {
                "name": "Marketing Executive Review",
                "display_order": 1,
                "parallel_mode": "SINGLE",
                "on_rejection_action": "TERMINATE",
                "goto_order": None,
                "steps": [
                    {
                        "name": "Marketing Executive Review",
                        "display_order": 1,
                        "required_role": "marketing_executive",
                        "scope_resolution_policy": "SUBJECT_NODE",
                        "default_user": "Arjun.punjabi@hiparks.com",
                        "step_kind": "NORMAL_APPROVAL",
                    }
                ],
            },
            {
                "name": "Runtime Split Allocation",
                "display_order": 2,
                "parallel_mode": "SINGLE",
                "on_rejection_action": "BRANCH_CORRECTION",
                "goto_order": None,
                "steps": [
                    {
                        "name": "Runtime Split Allocation",
                        "display_order": 1,
                        "required_role": "marketing_executive",
                        "scope_resolution_policy": "SUBJECT_NODE",
                        "default_user": "Arjun.punjabi@hiparks.com",
                        "step_kind": "RUNTIME_SPLIT_ALLOCATION",
                        "allocation_total_policy": "MUST_EQUAL_INVOICE_TOTAL",
                        "approver_selection_mode": "RUNTIME_SELECTED_FROM_POOL",
                        "require_category": True,
                        "require_budget": True,
                        "allow_multiple_lines_per_entity": False,
                        "branch_approval_policy": "REQUIRED_FOR_ALL",
                        "split_entities": [
                            ("Corporate", ["Taruna.Mahajan@hiparks.com"]),
                            ("North", ["Taruna.Mahajan@hiparks.com"]),
                            ("South", ["Taruna.Mahajan@hiparks.com"]),
                            ("West", ["Taruna.Mahajan@hiparks.com"]),
                            ("Incity", ["Taruna.Mahajan@hiparks.com"]),
                        ],
                    }
                ],
            },
        ],
    },
    {
        "code": "invoice-send-to-kajal",
        "name": "Invoice - Send To Kajal",
        "route": ("send-to-kajal", "Send To Kajal", 4),
        "version_number": 2,
        "groups": [
            {
                "name": "Marketing Executive Review",
                "display_order": 1,
                "parallel_mode": "SINGLE",
                "on_rejection_action": "TERMINATE",
                "goto_order": None,
                "steps": [
                    {
                        "name": "Marketing Executive Review",
                        "display_order": 1,
                        "required_role": "marketing_executive",
                        "scope_resolution_policy": "SUBJECT_NODE",
                        "default_user": "kajal.tiwari@hiparks.com",
                        "step_kind": "NORMAL_APPROVAL",
                    }
                ],
            },
            {
                "name": "Runtime Split Allocation",
                "display_order": 2,
                "parallel_mode": "SINGLE",
                "on_rejection_action": "BRANCH_CORRECTION",
                "goto_order": None,
                "steps": [
                    {
                        "name": "Runtime Split Allocation",
                        "display_order": 1,
                        "required_role": "marketing_executive",
                        "scope_resolution_policy": "SUBJECT_NODE",
                        "default_user": "kajal.tiwari@hiparks.com",
                        "step_kind": "RUNTIME_SPLIT_ALLOCATION",
                        "allocation_total_policy": "MUST_EQUAL_INVOICE_TOTAL",
                        "approver_selection_mode": "RUNTIME_SELECTED_FROM_POOL",
                        "require_category": True,
                        "require_budget": True,
                        "allow_multiple_lines_per_entity": False,
                        "branch_approval_policy": "REQUIRED_FOR_ALL",
                        "split_entities": [
                            ("Corporate", ["Taruna.Mahajan@hiparks.com"]),
                            ("North", ["Taruna.Mahajan@hiparks.com"]),
                            ("South", ["Taruna.Mahajan@hiparks.com"]),
                            ("West", ["Taruna.Mahajan@hiparks.com"]),
                            ("Incity", ["Taruna.Mahajan@hiparks.com"]),
                        ],
                    }
                ],
            },
        ],
    },
]


class Command(BaseCommand):
    help = "Seed Horizon UAT config state (users, roles, budgets, workflows, routes) without vendor/invoice runtime data."

    @transaction.atomic
    def handle(self, *args, **options):
        org = self._ensure_org_and_scopes()
        roles = self._ensure_roles(org)
        users = self._ensure_users_and_assignments(org, roles)
        self._seed_budget_taxonomy(org)
        self._seed_budgets(org)
        self._seed_workflows_and_routes(org, roles, users)
        self.stdout.write(self.style.SUCCESS("Horizon UAT server seed completed."))
        self.stdout.write(f"Default password set on seeded users: {PASSWORD}")

    def _ensure_org_and_scopes(self):
        org, _ = Organization.objects.get_or_create(code="horizon", defaults={"name": "Horizon"})
        org.name = "Horizon"
        org.save(update_fields=["name"])

        marketing, _ = ScopeNode.objects.get_or_create(
            org=org,
            code="marketing",
            defaults={
                "name": "Marketing",
                "node_type": "department",
                "parent": None,
                "path": f"/horizon/marketing",
                "depth": 0,
                "is_active": True,
            },
        )
        marketing.name = "Marketing"
        marketing.node_type = "department"
        marketing.parent = None
        marketing.path = f"/horizon/marketing"
        marketing.depth = 0
        marketing.is_active = True
        marketing.save(update_fields=["name", "node_type", "parent", "path", "depth", "is_active"])

        for name in ("Corporate", "North", "South", "West", "Incity"):
            code = name.lower()
            node, _ = ScopeNode.objects.get_or_create(
                org=org,
                code=code,
                defaults={
                    "name": name,
                    "node_type": "region",
                    "parent": marketing,
                    "path": f"/horizon/marketing/{code}",
                    "depth": 1,
                    "is_active": True,
                },
            )
            node.name = name
            node.node_type = "region"
            node.parent = marketing
            node.path = f"/horizon/marketing/{code}"
            node.depth = 1
            node.is_active = True
            node.save(update_fields=["name", "node_type", "parent", "path", "depth", "is_active"])
        return org

    def _ensure_roles(self, org):
        roles = {}
        for code, spec in ROLE_SPECS.items():
            role, _ = Role.objects.get_or_create(
                org=org,
                code=code,
                defaults={"name": spec["name"], "is_active": True},
            )
            role.name = spec["name"]
            role.is_active = True
            role.save(update_fields=["name", "is_active"])
            RolePermission.objects.filter(role=role).delete()
            for action, resource in spec["permissions"]:
                perm, _ = Permission.objects.get_or_create(action=action, resource=resource)
                RolePermission.objects.get_or_create(role=role, permission=perm)
            roles[code] = role
        return roles

    def _ensure_users_and_assignments(self, org, roles):
        marketing = ScopeNode.objects.get(org=org, name="Marketing")
        users = {}
        for email, first, last, is_staff, role_code in USER_SPECS:
            user, _ = User.objects.get_or_create(
                email=email,
                defaults={
                    "first_name": first,
                    "last_name": last,
                    "is_active": True,
                    "is_staff": is_staff,
                },
            )
            user.first_name = first
            user.last_name = last
            user.is_active = True
            user.is_staff = is_staff
            user.set_password(PASSWORD)
            user.save()
            UserRoleAssignment.objects.update_or_create(
                user=user,
                role=roles[role_code],
                scope_node=marketing,
                defaults={},
            )
            users[email] = user
        return users

    def _seed_budget_taxonomy(self, org):
        categories = {}
        for code, name in CATEGORY_SPECS:
            cat, _ = BudgetCategory.objects.get_or_create(
                org=org,
                code=code,
                defaults={"name": name, "is_active": True},
            )
            cat.name = name
            cat.is_active = True
            cat.save(update_fields=["name", "is_active"])
            categories[code] = cat

        for category_code, code, name in SUBCATEGORY_SPECS:
            subcat, _ = BudgetSubCategory.objects.get_or_create(
                category=categories[category_code],
                code=code,
                defaults={"name": name, "is_active": True},
            )
            subcat.name = name
            subcat.is_active = True
            subcat.save(update_fields=["name", "is_active"])

    def _seed_budgets(self, org):
        BudgetLine.objects.filter(budget__org=org).delete()
        Budget.objects.filter(org=org).delete()

        categories = {c.code: c for c in BudgetCategory.objects.filter(org=org)}
        subcategories = {(s.category.code, s.code): s for s in BudgetSubCategory.objects.select_related("category").filter(category__org=org)}
        scopes = {s.name: s for s in ScopeNode.objects.filter(org=org)}
        budgets = {}

        for code, name, scope_name, fy, period_type, currency, status in BUDGET_SPECS:
            budget = Budget.objects.create(
                org=org,
                scope_node=scopes[scope_name],
                name=name,
                code=code,
                financial_year=fy,
                period_type=period_type,
                currency=currency,
                status=status,
            )
            budgets[code] = budget

        for budget_code, category_code, subcategory_code, allocated, reserved, consumed in BUDGET_LINE_SPECS:
            BudgetLine.objects.create(
                budget=budgets[budget_code],
                category=categories[category_code],
                subcategory=subcategories.get((category_code, subcategory_code)) if subcategory_code else None,
                allocated_amount=allocated,
                reserved_amount=reserved,
                consumed_amount=consumed,
            )

        for budget in budgets.values():
            lines = list(budget.lines.all())
            budget.allocated_amount = sum(line.allocated_amount for line in lines)
            budget.reserved_amount = sum(line.reserved_amount for line in lines)
            budget.consumed_amount = sum(line.consumed_amount for line in lines)
            budget.save(update_fields=["allocated_amount", "reserved_amount", "consumed_amount"])

    def _seed_workflows_and_routes(self, org, roles, users):
        marketing = ScopeNode.objects.get(org=org, name="Marketing")
        VendorSubmissionRoute.objects.filter(org=org).delete()
        WorkflowSplitOption.objects.filter(workflow_step__group__template_version__template__scope_node=marketing).delete()
        WorkflowStep.objects.filter(group__template_version__template__scope_node=marketing).delete()
        StepGroup.objects.filter(template_version__template__scope_node=marketing).delete()
        WorkflowTemplateVersion.objects.filter(template__scope_node=marketing, template__module="invoice").delete()
        WorkflowTemplate.objects.filter(scope_node=marketing, module="invoice").delete()

        for wf in WORKFLOW_SPECS:
            template = WorkflowTemplate.objects.create(
                name=wf["name"],
                code=wf["code"],
                description="",
                module="invoice",
                scope_node=marketing,
                is_active=True,
                is_default=False,
                created_by=users["HorizonAdmin@hiparks.com"],
            )
            version = WorkflowTemplateVersion.objects.create(
                template=template,
                version_number=wf["version_number"],
                status=VersionStatus.PUBLISHED,
                published_at=timezone.now(),
                published_by=users["HorizonAdmin@hiparks.com"],
            )

            groups_by_order = {}
            for group_spec in wf["groups"]:
                group = StepGroup.objects.create(
                    template_version=version,
                    name=group_spec["name"],
                    display_order=group_spec["display_order"],
                    parallel_mode=group_spec["parallel_mode"],
                    on_rejection_action=group_spec["on_rejection_action"],
                )
                groups_by_order[group.display_order] = group

            for group_spec in wf["groups"]:
                group = groups_by_order[group_spec["display_order"]]
                goto_order = group_spec["goto_order"]
                if goto_order is not None:
                    group.on_rejection_goto_group = groups_by_order[goto_order]
                    group.save(update_fields=["on_rejection_goto_group"])

                for step_spec in group_spec["steps"]:
                    step = WorkflowStep.objects.create(
                        group=group,
                        name=step_spec["name"],
                        required_role=roles[step_spec["required_role"]],
                        scope_resolution_policy=step_spec["scope_resolution_policy"],
                        ancestor_node_type="",
                        fixed_scope_node=None,
                        default_user=users[step_spec["default_user"]],
                        display_order=step_spec["display_order"],
                        step_kind=step_spec["step_kind"],
                        split_target_nodes=[],
                        split_target_mode="",
                        join_policy="",
                        allocation_total_policy=step_spec.get("allocation_total_policy", "MUST_EQUAL_INVOICE_TOTAL"),
                        approver_selection_mode=step_spec.get("approver_selection_mode", "RUNTIME_SELECTED_FROM_POOL"),
                        require_category=step_spec.get("require_category", False),
                        require_subcategory=step_spec.get("require_subcategory", False),
                        require_budget=step_spec.get("require_budget", False),
                        require_campaign=step_spec.get("require_campaign", False),
                        allow_multiple_lines_per_entity=step_spec.get("allow_multiple_lines_per_entity", False),
                        branch_approval_policy=step_spec.get("branch_approval_policy", "REQUIRED_FOR_ALL"),
                    )
                    for idx, (entity_name, approver_emails) in enumerate(step_spec.get("split_entities", []), start=1):
                        option = WorkflowSplitOption.objects.create(
                            workflow_step=step,
                            entity=ScopeNode.objects.get(org=org, name=entity_name),
                            approver_role=None,
                            category=None,
                            subcategory=None,
                            campaign=None,
                            budget=None,
                            is_active=True,
                            display_order=idx,
                        )
                        option.allowed_approvers.set([users[email] for email in approver_emails])

            route_code, route_label, route_order = wf["route"]
            VendorSubmissionRoute.objects.create(
                org=org,
                code=route_code,
                label=route_label,
                description="",
                display_order=route_order,
                is_active=True,
                workflow_template=template,
            )
